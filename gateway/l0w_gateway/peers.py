"""Friends and messages in the gateway (protocol/messages.md): pinned keys, delivery tokens, the outbox, opening envelopes.

peers.json, by lowercase address:
  {address, sign_key (the pinned key object), old_keys (earlier pinned keys, oldest first), recover_key,
   sign_seq (highest self-description seq seen), seal_key, hostnames, fetched_at (epoch),
   issued_token ({doc, id, revoked}: our token for them) | None, held_token (their token doc for us) | None, seq_out}
outbox.json: [{id, to, kind, doc, host, seq, created, attempts, next_at, state: queued|delivered|failed, error, final_at, updated}]
hubs.json: {hub domain: signing key object} pinned on first contact.
services.json: [address...] the addresses control says are hub services (services.md); `open` takes `service-*` envelopes from those only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from l0wproto import b64, docs, keys, messages
from l0wproto import payloads as P
from l0wproto.consts import ENVELOPE_MAX_AGE, MAX_ENVELOPE
from l0wproto.errors import ProtocolError, SealError, SignatureError
from l0wproto.keys import Key
from l0wproto.times import format_time, parse_time

from .safefetch import PinnedAsyncTransport
from .store import Store

log = logging.getLogger("l0w_gateway.peers")

RETIRED_SEAL_KEEP = ENVELOPE_MAX_AGE  # 8 days: envelopes sealed to the old key still open
OUTBOX_MAX_AGE = 7 * 24 * 3600
FINISHED_KEEP = 14 * 24 * 3600
BACKOFF_FIRST, BACKOFF_MAX = 30, 3600
DIRECTORY_FRESH = 300  # seconds a pinned directory entry's hostnames are trusted before a refresh at delivery
INLINE_TIMEOUT, WORKER_TIMEOUT = 5.0, 20.0
MAX_DIRECTORY_BYTES = 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra):
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


class NetError(Exception):
    pass


class Net:
    """The gateway's outgoing HTTP. Every request is vetted like egress (https, public addresses only, connect to
    the vetted address, no redirects) unless a development flag is set: `L0W_HUB_ALLOW_HTTP=1` (http allowed, and no
    vetting, so a fake hub on a private address works) and `L0W_HUB_CONNECT_TO=host:port` (every request goes there,
    with the real Host header). To reach several fake hubs, the value may be a comma-separated list of `domain=host:port`
    entries (a request to that domain or any name under it goes to that address) with an optional bare `host:port` last,
    for everything else. `transport` replaces everything (tests); `inner` is what the vetted transport wraps."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, inner: httpx.AsyncBaseTransport | None = None,
                 connect_to: str | None = None, allow_http: bool | None = None):
        self.connect_to = connect_to if connect_to is not None else os.environ.get("L0W_HUB_CONNECT_TO") or None
        self.allow_http = allow_http if allow_http is not None else os.environ.get("L0W_HUB_ALLOW_HTTP") == "1"
        self.dev = self.allow_http or bool(self.connect_to)
        self.transport = transport
        self.inner = inner
        self._routes: list[tuple[str, str]] = []  # (domain, host:port) from the `domain=host:port` entries
        self._default: str | None = None
        for part in (self.connect_to or "").split(","):
            part = part.strip()
            if "=" in part:
                domain, _, addr = part.partition("=")
                self._routes.append((domain.strip().lower(), addr.strip()))
            elif part:
                self._default = part

    def _connect_for(self, netloc: str) -> str | None:
        """Where a request to this host (with its port) goes in development, or None to go where the URL says."""
        host = (urlsplit(f"//{netloc}").hostname or "").lower()
        for domain, addr in self._routes:
            if host == domain or host.endswith("." + domain):
                return addr
        return self._default

    @property
    def scheme(self) -> str:
        return "http" if self.allow_http else "https"

    def _transport(self) -> httpx.AsyncBaseTransport | None:
        if self.transport is not None:
            return self.transport
        if self.dev:
            return self.inner
        return PinnedAsyncTransport(self.inner)

    async def request(self, method: str, url: str, *, content: bytes | None = None, headers: dict | None = None,
                      timeout: float = 10.0, limit: int = MAX_DIRECTORY_BYTES) -> tuple[int, dict, bytes]:
        """-> (status, headers, body up to `limit` bytes). Raises NetError."""
        u = urlsplit(url)
        if u.scheme != "https" and not (self.allow_http and u.scheme == "http"):
            raise NetError("only https is allowed")
        hdrs = dict(headers or {})
        target = self._connect_for(u.netloc) if self.connect_to else None
        if target:
            hdrs["Host"] = u.netloc
            url = u._replace(netloc=target).geturl()
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport(), follow_redirects=False) as c:
                async with c.stream(method, url, content=content, headers=hdrs) as r:
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > limit:
                            raise NetError("the answer is too large")
                    return r.status_code, dict(r.headers), bytes(buf)
        except httpx.HTTPError as e:
            raise NetError(str(e) or e.__class__.__name__) from None


def _norm_fp(fp: str) -> str:
    return fp.replace("-", "").upper()


def _kp(entry: dict, use: str) -> keys.KeyPair:
    return keys.from_seed(b64.decode(entry["seed"]), use, entry["pub"]["created"])


def _addr(value: object, field: str = "address") -> str:
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be an address")
    try:
        name, domain = P.split_address(value)
    except ProtocolError as e:
        raise ApiError(400, "bad_request", f"{field}: {e.detail}") from None
    return f"{name}@{domain}"


def _kids(peer: dict) -> list[str]:
    return [k["kid"] for k in peer["old_keys"]] + [peer["sign_key"]["kid"]]


def walk_rotations(start: Key, target_kid: str, history: list, address: str, recovery: Key | None, now: int) -> list[Key] | None:
    """The keys after `start` up to the one named `target_kid`, following signed rotations, or None when the
    history doesn't lead there. Each rotation is signed by the key it replaces (or by the pinned recovery key)."""
    cur, out = start, []
    for _ in range(len(history) + 1):
        if cur.kid == target_kid:
            return out
        for doc in history:
            try:
                if docs.parse_payload(doc, now).get("old") != cur.kid:
                    continue
                p = P.verify_rotation(doc, cur, recovery, now)
                new = Key.from_dict(p["new"])
            except (ProtocolError, ValueError, KeyError, TypeError):
                continue
            if new.alg != "Ed25519" or new.use != "sign" or p["address"].lower() != address:
                continue
            cur = new
            out.append(new)
            break
        else:
            return None
    return None


class Peers:
    def __init__(self, store: Store, net: Net, clock: Callable[[], float] = time.time, backoff_first: int = BACKOFF_FIRST):
        self.store, self.net, self.clock = store, net, clock
        self.backoff_first = backoff_first  # seconds before the first retry (doubling to BACKOFF_MAX); `GATEWAY_OUTBOX_MIN_DELAY`
        self.inflight: set[str] = set()

    def now(self) -> int:
        return int(self.clock())

    # --- our own keys ---

    def _keys(self) -> dict:
        st = self.store.keys()
        if not st:
            raise ApiError(409, "no_keys", "no keys yet: register first")
        return st

    def own_address(self) -> str:
        cfg = self.store.tunnel()
        if not cfg or not cfg.get("address"):
            raise ApiError(409, "no_tunnel", "no address yet: join a hub first")
        return cfg["address"].lower()

    def sign_pair(self) -> keys.KeyPair:
        return _kp(self._keys()["sign"], "sign")

    def seal_seeds(self) -> dict[str, bytes]:
        """Our current encryption key and the ones retired in the last 8 days, by kid."""
        st = self._keys()
        out = {st["seal"]["pub"]["kid"]: b64.decode(st["seal"]["seed"])}
        for r in st.get("retired_seal", []):
            if parse_time(r["retired_at"]) >= self.now() - RETIRED_SEAL_KEEP:
                out[r["pub"]["kid"]] = b64.decode(r["seed"])
        return out

    def fingerprint(self) -> str:
        return messages.fingerprint(self.sign_pair().key.pub)

    # --- the directory ---

    async def _get_json(self, url: str, what: str) -> tuple[int, object]:
        try:
            status, _h, body = await self.net.request("GET", url, headers={"Accept": "application/json"})
        except NetError as e:
            raise ApiError(502, "unreachable", f"couldn't reach {urlsplit(url).netloc} for {what}: {e}") from None
        try:
            return status, json.loads(body) if body else None
        except ValueError:
            return status, None

    async def _hub_keys(self, domain: str) -> list[Key]:
        status, doc = await self._get_json(f"{self.net.scheme}://{domain}/.well-known/l0wagent/hub", "the hub's capabilities")
        if status != 200 or not isinstance(doc, dict):
            raise ApiError(502, "hub_capabilities", f"the hub's capabilities answered {status}")
        pins = self.store.hubs()
        pinned = Key.from_dict(pins[domain]) if domain in pins else None
        try:
            p = P.verify_hub(doc, pinned=pinned)
        except SignatureError as e:
            if pinned is not None and "pinned" in str(e):
                raise ApiError(409, "hub_key_changed", f"{domain}'s signing key is not the one first seen") from None
            raise ApiError(502, "hub_capabilities", f"the hub's capabilities don't verify: {e.detail}") from None
        except ProtocolError as e:
            raise ApiError(502, "hub_capabilities", f"the hub's capabilities don't verify: {e.detail}") from None
        sign_keys = [k for k in (Key.from_dict(x) for x in p["keys"]) if k.use == "sign" and k.alg == "Ed25519"]
        if pinned is None:
            with self.store.lock:
                hubs = self.store.hubs()
                hubs.setdefault(domain, sign_keys[0].to_dict())
                self.store.save_hubs(hubs)
        return sign_keys

    async def fetch_entry(self, address: str) -> dict:
        """The verified self-description payload from the directory (the hub's capabilities first)."""
        name, domain = P.split_address(address)
        hub_keys = await self._hub_keys(domain)
        status, entry = await self._get_json(f"{self.net.scheme}://{domain}/.well-known/l0wagent/members/{name}", "the directory")
        if status == 404:
            raise ApiError(404, "no_such_member", f"{address} isn't in the hub's directory")
        if status != 200 or not isinstance(entry, dict):
            raise ApiError(502, "bad_directory", f"the directory answered {status}")
        hub_sig = entry.get("hub_sig")
        hk = next((k for k in hub_keys if isinstance(hub_sig, dict) and k.kid == hub_sig.get("kid")), None)
        if hk is None:
            raise ApiError(502, "bad_directory", "the directory entry isn't signed by one of the hub's keys")
        try:
            return docs.verify_directory_entry(entry, hk, hub_domain=domain, name=name)
        except ProtocolError as e:
            raise ApiError(502, "bad_directory", f"the directory entry doesn't verify: {e.detail}") from None

    @staticmethod
    def _entry_keys(p: dict) -> dict[str, Key]:
        return {k.use: k for k in (Key.from_dict(x) for x in p["keys"])}

    # --- pinning ---

    async def pin(self, address: str, fingerprint: str | None = None, held_token: dict | None = None) -> dict:
        address = _addr(address)
        p = await self.fetch_entry(address)
        ks = self._entry_keys(p)
        new_sign, new_seal, new_rec = ks["sign"], ks["seal"], ks["recover"]
        fp = messages.fingerprint(new_sign.pub)
        if fingerprint is not None and _norm_fp(fingerprint) != _norm_fp(fp):
            raise ApiError(409, "fingerprint_mismatch", "the key in the directory isn't the one you were given", fingerprint=fp)
        rotated = False
        with self.store.lock:
            peers = self.store.peers()
            cur = peers.get(address)
            if cur is None:
                cur = peers[address] = {
                    "address": address, "sign_key": new_sign.to_dict(), "old_keys": [], "recover_key": new_rec.to_dict(), "sign_seq": p["seq"],
                    "seal_key": new_seal.to_dict(), "hostnames": p["hostnames"], "fetched_at": self.now(),
                    "issued_token": None, "held_token": None, "seq_out": 0,
                }
            elif p["seq"] >= cur["sign_seq"]:
                pinned = Key.from_dict(cur["sign_key"])
                if pinned.kid != new_sign.kid:
                    chain = walk_rotations(pinned, new_sign.kid, p["history"], address, Key.from_dict(cur["recover_key"]), self.now())
                    if chain is None:
                        raise ApiError(409, "key_changed", f"{address}'s signing key changed without a rotation you can follow",
                                       old_fingerprint=messages.fingerprint(pinned.pub), new_fingerprint=fp)
                    cur["old_keys"] += [pinned.to_dict()] + [k.to_dict() for k in chain[:-1]]
                    cur["sign_key"] = chain[-1].to_dict()
                    rotated = True
                cur.update(seal_key=new_seal.to_dict(), hostnames=p["hostnames"], sign_seq=p["seq"], fetched_at=self.now())
            if held_token is not None:
                self._set_held(cur, held_token)
            self.store.save_peers(peers)
            return {"address": address, "fingerprint": messages.fingerprint(Key.from_dict(cur["sign_key"]).pub), "hostnames": cur["hostnames"],
                    "rotated": rotated, "sign_key": cur["sign_key"], "seal_key": cur["seal_key"]}

    def _set_held(self, peer: dict, token: dict) -> None:
        try:
            tp = P.validate_token_document(token)
        except ProtocolError as e:
            raise ApiError(400, "bad_token", e.detail) from None
        if tp["issuer"].lower() != peer["address"] or tp["holder"].lower() != self.own_address():
            raise ApiError(400, "bad_token", "that token isn't from this friend to us")
        issuer = next((Key.from_dict(k) for k in [peer["sign_key"], *peer["old_keys"]] if k["kid"] == tp["issuer_kid"]), None)
        if issuer is None:
            raise ApiError(400, "bad_token", "that token was signed by a key this friend hasn't been seen with")
        try:
            messages.verify_delivery_token(token, issuer, now=self.now())
        except ProtocolError as e:
            raise ApiError(400, "bad_token", e.detail) from None
        peer["held_token"] = token

    def set_services(self, addresses: object) -> dict:
        """Control's list of the hub's service addresses (from the capabilities): the only senders of `service-*` envelopes."""
        if not isinstance(addresses, list):
            raise ApiError(400, "bad_request", "addresses must be a list")
        cleaned = sorted({_addr(a, "addresses") for a in addresses})
        with self.store.lock:
            self.store.save_services(cleaned)
        return {"addresses": cleaned}

    def _peer(self, peers: dict, address: str) -> dict:
        peer = peers.get(address)
        if peer is None:
            raise ApiError(409, "not_pinned", f"{address} isn't pinned: pin them first")
        return peer

    def issue_token(self, address: str) -> dict:
        address = _addr(address)
        with self.store.lock:
            peers = self.store.peers()
            peer = self._peer(peers, address)
            self._issue(peer)
            self.store.save_peers(peers)
            return {"address": address, "token": peer["issued_token"]["doc"]}

    def _issue(self, peer: dict) -> dict:
        it = peer["issued_token"]
        if it and not it["revoked"]:
            return it["doc"]
        kp = self.sign_pair()
        payload = P.build_delivery_token(self.own_address(), kp.kid, peer["address"], peer["sign_key"]["kid"], iat=format_time(self.now()))
        doc = docs.sign_document("delivery-token", payload, [kp])
        peer["issued_token"] = {"doc": doc, "id": payload["id"], "revoked": False}
        return doc

    def revoke(self, address: str) -> dict:
        address = _addr(address)
        with self.store.lock:
            peers = self.store.peers()
            peer = peers.get(address)
            if peer and peer["issued_token"]:
                peer["issued_token"]["revoked"] = True
                self.store.save_peers(peers)
            return {"address": address, "revoked": bool(peer and peer["issued_token"])}

    def forget(self, address: str) -> dict:
        address = _addr(address)
        with self.store.lock:
            peers = self.store.peers()
            had = peers.pop(address, None) is not None
            self.store.save_peers(peers)
            return {"address": address, "forgotten": had}

    # --- sending ---

    async def send(self, to: str, kind: str, body: object, request_token: bool = False) -> dict:
        to = _addr(to, "to")
        if not isinstance(kind, str) or kind not in P.KINDS:
            raise ApiError(422, "unknown_kind", f"unknown kind {kind!r}")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "body must be an object")
        if request_token and kind not in ("friend-request", "friend-accept", "service-hello"):
            raise ApiError(400, "bad_request", "request_token only applies to friend-request, friend-accept and service-hello")
        with self.store.lock:
            peers = self.store.peers()
            peer = self._peer(peers, to)
            kp = self.sign_pair()
            own = self.own_address()
            body = dict(body)
            if kind in ("friend-request", "friend-accept", "service-hello") and (request_token or body.get("token") is None):
                body["token"] = self._issue(peer)
            token_doc = None
            if kind not in P.TOKENLESS_KINDS:
                token_doc = peer["held_token"]
                if token_doc is None:
                    raise ApiError(409, "no_token", f"{to} hasn't given us a delivery token")
            seq = peer["seq_out"] + 1
            try:
                doc = messages.make_envelope(kp, kp.kid, own, to, Key.from_dict(peer["seal_key"]), kind, body, seq, token_doc,
                                             iat=format_time(self.now()))
            except ProtocolError as e:
                raise ApiError(400 if e.code != "too_large" else 413, "bad_body" if e.code == "bad_document" else e.code, e.detail) from None
            peer["seq_out"] = seq
            self.store.save_peers(peers)
            env_id = messages.envelope_payload(doc)["id"]
            entry = {"id": env_id, "to": to, "kind": kind, "doc": doc, "host": peer["hostnames"]["instance"], "seq": seq,
                     "created": format_time(self.now()), "attempts": 0, "next_at": format_time(self.now()), "state": "queued", "error": None,
                     "final_at": None, "updated": format_time(self.now())}
            outbox = self.store.outbox()
            if kind == "presence":  # newer presence replaces older unsent presence to the same friend
                outbox = [e for e in outbox if not (e["to"] == to and e["kind"] == "presence" and e["state"] == "queued" and e["id"] not in self.inflight)]
            outbox.append(entry)
            self.store.save_outbox(outbox)
        await self.attempt(env_id, INLINE_TIMEOUT)
        with self.store.lock:
            done = next(e for e in self.store.outbox() if e["id"] == env_id)
        return {"id": env_id, "state": done["state"], "error": done["error"]}

    def outbox_view(self, since: str | None) -> list[dict]:
        if since is not None:
            parse_time(since)
        with self.store.lock:
            return [{k: v for k, v in e.items() if k not in ("doc", "host")} for e in self.store.outbox() if since is None or e["updated"] >= since]

    # --- delivery ---

    def _finish(self, env_id: str, **changes) -> None:
        with self.store.lock:
            outbox = self.store.outbox()
            for e in outbox:
                if e["id"] == env_id and e["state"] == "queued":
                    e.update(changes, updated=format_time(self.now()))
            self.store.save_outbox(outbox)

    async def attempt(self, env_id: str, timeout: float) -> None:
        """One delivery attempt of a queued entry; records the outcome."""
        with self.store.lock:
            if env_id in self.inflight:
                return
            e = next((x for x in self.store.outbox() if x["id"] == env_id and x["state"] == "queued"), None)
            if e is None:
                return
            e = dict(e)
            self.inflight.add(env_id)
        try:
            now = self.now()
            if now - parse_time(e["created"]) >= OUTBOX_MAX_AGE:
                self._finish(env_id, state="failed", error="expired", final_at=format_time(now))
                return
            host = await self._host_for(e)
            status, retry_after, error = None, None, None
            try:
                status, hdrs, body = await self.net.request("POST", f"{self.net.scheme}://{host}/api/peer/v0/inbox", content=json.dumps(e["doc"], separators=(",", ":")).encode(),
                                                            headers={"Content-Type": "application/json"}, timeout=timeout)
            except NetError as err:
                error = f"unreachable: {err}"[:300]
            now = self.now()
            if status is not None and 200 <= status < 300:
                self._finish(env_id, state="delivered", error=None, final_at=format_time(now), attempts=e["attempts"] + 1)
            elif status is not None and 400 <= status < 500 and status not in (408, 429):
                code = None
                try:
                    code = json.loads(body).get("error")
                except (ValueError, AttributeError):
                    pass
                self._finish(env_id, state="failed", error=str(code or f"http_{status}")[:100], final_at=format_time(now), attempts=e["attempts"] + 1)
            else:
                if status is not None:
                    error = f"http_{status}"
                    try:
                        retry_after = int(hdrs.get("retry-after", ""))
                    except ValueError:
                        pass
                n = e["attempts"] + 1
                delay = min(self.backoff_first * 2 ** (n - 1), BACKOFF_MAX)
                if retry_after:
                    delay = min(max(delay, retry_after), BACKOFF_MAX)
                self._finish(env_id, error=error, attempts=n, next_at=format_time(now + delay))
        finally:
            self.inflight.discard(env_id)

    async def _host_for(self, e: dict) -> str:
        """The recipient's instance hostname: refreshed from the directory when the pinned copy is old, else what's pinned."""
        with self.store.lock:
            peer = self.store.peers().get(e["to"])
        if peer is None:
            return e["host"]
        if self.now() - peer["fetched_at"] > DIRECTORY_FRESH:
            try:
                await self.pin(e["to"])
            except Exception as err:  # best effort: a key change, an outage or a missing entry keeps the pinned hostname
                log.info("directory refresh for %s failed: %s", e["to"], getattr(err, "code", err))
            with self.store.lock:
                peer = self.store.peers().get(e["to"]) or peer
        return peer["hostnames"]["instance"]

    async def pump(self) -> int:
        """Attempt every queued entry that is due, then drop finished entries older than 14 days. -> attempts made."""
        now = self.now()
        with self.store.lock:
            outbox = self.store.outbox()
            keep = [e for e in outbox if e["state"] == "queued" or e["final_at"] is None or parse_time(e["final_at"]) > now - FINISHED_KEEP]
            if len(keep) != len(outbox):
                self.store.save_outbox(keep)
            due = [e["id"] for e in keep if e["state"] == "queued" and parse_time(e["next_at"]) <= now]
        for env_id in due:
            await self.attempt(env_id, WORKER_TIMEOUT)
        return len(due)

    async def run(self, interval: float = 5.0) -> None:
        while True:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox pump failed")
            await asyncio.sleep(interval)

    # --- receiving ---

    async def open(self, raw: bytes) -> dict:
        """Spec checks 2 to 6 (not the duplicate id). Changes state only to follow a sender's rotation and to keep a friend-accept's token."""
        if len(raw) > MAX_ENVELOPE:
            raise ApiError(413, "too_large", "the envelope is over 16 MiB")
        try:
            doc = json.loads(raw)
            if self._tokenless(doc, self.own_address()):  # the shape check below would call this a bad document; the spec says 403
                raise ApiError(403, "no_token", "")
            p = messages.envelope_payload(doc)
        except ValueError:
            raise ApiError(400, "bad_document", "not JSON") from None
        except ProtocolError as e:
            raise ApiError(400, e.code, e.detail) from None
        own = self.own_address()
        if P.split_address(p["to"]) != P.split_address(own):
            raise ApiError(404, "not_here", "")
        sender, kind = p["from"].lower(), p["kind"]
        with self.store.lock:
            is_service = sender in self.store.services()
        if kind in P.SERVICE_KINDS and not is_service:
            raise ApiError(403, "no_token", "")  # a service kind from anyone control hasn't named a service
        if is_service and kind not in P.SERVICE_KINDS:
            raise ApiError(403, "no_token", "")  # and a service sends nothing but service kinds
        if kind != "friend-request":
            self._check_token(sender, p)
        key = await self._sender_key(sender, p["from_kid"], friend_request=kind == "friend-request")
        try:
            payload, body = messages.open_envelope(doc, my_address=own, my_seal_keys=self.seal_seeds(), sender_sign_key=key, now=self.now())
        except SignatureError:
            raise ApiError(401, "bad_signature", "") from None
        except SealError:
            raise ApiError(400, "bad_seal", "") from None
        except ProtocolError as e:
            status = {"not_here": 404, "unknown_kind": 422, "bad_body": 400, "bad_signature": 401, "bad_seal": 400}.get(e.code, 400)
            raise ApiError(status, e.code, "" if status == 401 else e.detail) from None
        if kind in ("friend-accept", "service-welcome"):
            with self.store.lock:
                peers = self.store.peers()
                peer = peers.get(sender)
                if peer is not None:
                    self._set_held(peer, body["token"])
                    self.store.save_peers(peers)
        return {"from": sender, "from_kid": payload["from_kid"], "fingerprint": messages.fingerprint(key.pub), "id": payload["id"],
                "seq": payload["seq"], "kind": kind, "iat": payload["iat"], "body": body}

    @staticmethod
    def _tokenless(doc: object, own: str) -> bool:
        """An envelope for us, of a kind that needs a token, carrying none."""
        try:
            p = json.loads(b64.decode(doc["payload"]))  # type: ignore[index]
            return p["kind"] != "friend-request" and not isinstance(p.get("token"), dict) and P.split_address(p["to"]) == P.split_address(own)
        except (ValueError, KeyError, TypeError, ProtocolError):
            return False

    def _check_token(self, sender: str, p: dict) -> None:
        deny = ApiError(403, "no_token", "")
        tok = p.get("token")
        if tok is None:
            raise deny
        with self.store.lock:
            peer = self.store.peers().get(sender)
        it = peer["issued_token"] if peer else None
        if not it or it["revoked"] or tok != it["doc"]:
            raise deny
        tp = json.loads(b64.decode(tok["payload"]))  # equal to the token we issued, so it is well formed
        if tp["holder"].lower() != sender or tp["holder_kid"] not in _kids(peer):
            raise deny

    async def _sender_key(self, sender: str, from_kid: str, *, friend_request: bool) -> Key:
        bad = ApiError(401, "bad_signature", "")
        if not friend_request:
            with self.store.lock:
                peer = self.store.peers().get(sender)
            if peer is None:
                raise ApiError(403, "no_token", "")
            if peer["sign_key"]["kid"] == from_kid:
                return Key.from_dict(peer["sign_key"])
            if from_kid in _kids(peer):
                raise bad  # a key the friend has rotated away from
        try:
            entry = await self.fetch_entry(sender)
        except ApiError as e:
            if e.status in (502, 404):
                raise ApiError(503, "unresolvable", f"couldn't look up {sender}: {e.detail}") from None
            raise
        new_sign = self._entry_keys(entry)["sign"]
        if friend_request:
            if new_sign.kid != from_kid:
                raise bad
            return new_sign
        with self.store.lock:
            peers = self.store.peers()
            peer = peers.get(sender)
            if peer is None:
                raise ApiError(403, "no_token", "")
            pinned = Key.from_dict(peer["sign_key"])
            chain = walk_rotations(pinned, from_kid, entry["history"], sender, Key.from_dict(peer["recover_key"]), self.now())
            if not chain:
                raise bad
            peer["old_keys"] += [pinned.to_dict()] + [k.to_dict() for k in chain[:-1]]
            peer["sign_key"] = chain[-1].to_dict()
            if entry["seq"] >= peer["sign_seq"]:
                peer.update(seal_key=self._entry_keys(entry)["seal"].to_dict(), hostnames=entry["hostnames"], sign_seq=entry["seq"], fetched_at=self.now())
            self.store.save_peers(peers)
            return chain[-1]
