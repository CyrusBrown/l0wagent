"""Membership of a hub (H3): joining, the self-description, the daily self-check, key rotation and
recovery, and leaving. The spec is protocol/membership.md and identity.md.

Control never holds the instance's private keys. The gateway (gateway/, on the `hubnet` network)
makes them, signs every document and hands out only the recovery key, once, for the owner. Control
talks to the hub over HTTPS itself, keeps the membership in `hub_membership`, serves the current
self-description at `/.well-known/l0wagent/self`, and tells the gateway the tunnel's settings (the
tunnel service reads them from the gateway).

Development: `L0W_HUB_ALLOW_HTTP=1` accepts http:// hubs (the fake hub), and `L0W_HUB_CONNECT_TO`
(host:port) sends every request for the hub and the member's hostnames to that address with the
real Host header, because `*.localhost` names don't resolve everywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from l0wproto import docs, names, payloads
from l0wproto.consts import VERSION
from l0wproto.errors import ProtocolError, SignatureError
from l0wproto.invite import parse_invite
from l0wproto.keys import Key

from . import __version__
from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, Conflict

log = logging.getLogger(__name__)
CHECK_EVERY = timedelta(hours=24)
TIMEOUT = 15.0


class GatewayError(Exception):
    def __init__(self, status: int, error: str, detail: str = ""):
        super().__init__(f"gateway {status} {error}: {detail}")
        self.status, self.error, self.detail = status, error, detail


class Gateway:
    """The gateway's control-side API (gateway/README.md)."""

    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.token, self.transport = url.rstrip("/"), token, transport

    async def call(self, method: str, path: str, body: dict | None = None, *, raw: bytes | None = None) -> dict:
        """`raw` sends those bytes as the body untouched (an incoming envelope, which the gateway must see exactly as it arrived)."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as c:
                r = await c.request(method, self.url + path, headers={"Authorization": f"Bearer {self.token}", **({"Content-Type": "application/json"} if raw is not None else {})},
                                    **({"content": raw} if raw is not None else {"json": body}))
        except httpx.HTTPError as e:
            raise GatewayError(0, "gateway_unreachable", str(e)) from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise GatewayError(r.status_code, data.get("error", "gateway_error"), data.get("detail", ""))
        return data


def accelerator_api(caps: dict | None, allow_http: bool = False) -> str | None:
    """The `api` of the hub's cloudflared accelerator extra (protocol/accelerators.md), or None when it isn't offered."""
    for e in (caps or {}).get("extras") or []:
        if isinstance(e, dict) and e.get("kind") == "accelerator" and e.get("id") == "cloudflared":
            api = e.get("api")
            if isinstance(api, str) and (api.startswith("https://") or (allow_http and api.startswith("http://"))):
                return api
    return None


def _example_name(ex: dict) -> str | None:
    """The name the hub's example hostnames were made for, if it gives a valid one (capabilities.md)."""
    try:
        return names.validate_name(ex["name"]) if isinstance(ex.get("name"), str) else None
    except ProtocolError:
        return None


def release_tuple(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in v.split("-")[0].split("."))
    except ValueError:
        return None


class Membership:
    def __init__(self, db, notifier, live, *, gateway: Gateway | None, two_factor: Callable[[], bool], now: Callable[[], datetime] = utcnow,
                 transport: httpx.AsyncBaseTransport | None = None, connect_to: str | None = None, allow_http: bool | None = None):
        self.db, self.notifier, self.live, self.gw = db, notifier, live, gateway
        self.two_factor, self.now, self.transport = two_factor, now, transport
        self.connect_to = connect_to if connect_to is not None else os.environ.get("L0W_HUB_CONNECT_TO") or None
        self.allow_http = allow_http if allow_http is not None else os.environ.get("L0W_HUB_ALLOW_HTTP") == "1"
        self._lock = asyncio.Lock()
        self.on_caps: Callable[[], Awaitable[None]] | None = None  # hub services follow the capabilities (hubservices.py)
        self.on_left: Callable[[], Awaitable[None]] | None = None
        self.on_policy: Callable[[], None] | None = None  # the owner changed how this hub's extension updates are handled (ops.py)

    # ── state ──────────────────────────────────────────────────────────────
    def row(self) -> dict | None:
        r = self.db.q1("SELECT * FROM hub_membership WHERE id=1")
        if not r:
            return None
        r = dict(r)
        for k in ("hub_key", "capabilities", "hostnames", "self_doc", "check_result"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        return r

    def accelerator_api(self, r: dict) -> str | None:
        return accelerator_api(r.get("capabilities"), self.allow_http or r["hub_base"].startswith("http://"))

    def joined(self) -> dict | None:
        r = self.row()
        return r if r and r["state"] == "joined" else None

    def _origin(self, r: dict, which: str) -> str:
        scheme = urlsplit(r["hub_base"]).scheme or "https"
        return f"{scheme}://{r['hostnames'][which]}"

    def instance_origin(self) -> str | None:
        r = self.joined()
        return self._origin(r, "instance") if r else None

    def files_origin(self) -> str | None:
        r = self.joined()
        return self._origin(r, "files") if r else None

    def files_host(self) -> str:
        r = self.joined()
        from .files_origin import host_of

        return host_of(self._origin(r, "files")) if r else ""

    def self_doc(self) -> dict | None:
        r = self.joined()
        return r["self_doc"] if r else None

    def _log(self, event: str, detail: str = "") -> None:
        self.db.ex("INSERT INTO hub_log(at, event, detail) VALUES(?,?,?)", (self.db.now(), event, detail[:1000]))

    def _changed(self) -> None:
        self.live.emit("membership")

    # ── HTTP to the hub and to the member's own hostnames ──────────────────
    async def _http(self, method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
        u = urlsplit(url)
        if u.scheme != "https" and not (self.allow_http and u.scheme == "http"):
            raise BadRequest("hub_insecure", "The hub must use https.")
        hdrs = dict(headers or {})
        if self.connect_to:
            hdrs["Host"] = u.netloc
            url = u._replace(scheme="http" if u.scheme == "http" else u.scheme, netloc=self.connect_to).geturl()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport, follow_redirects=False) as c:
                return await c.request(method, url, json=body, headers=hdrs)
        except httpx.HTTPError as e:
            raise BadRequest("hub_unreachable", f"Couldn't reach {u.netloc}: {e.__class__.__name__}") from e

    @staticmethod
    def _hub_error(r: httpx.Response) -> BadRequest | Conflict:
        try:
            data = r.json()
        except ValueError:
            data = {}
        code, detail = data.get("error") or f"hub_{r.status_code}", data.get("detail") or ""
        friendly = {
            "name_taken": "That name is taken on this hub. Names are never reused; pick another.",
            "name_reserved": "That name is reserved on this hub (or looks too much like one that is). Pick another.",
            "name_bound": "This invite is for one particular name. Use the name it came with.",
            "invite_used": "That invite was already used. Ask the hub's operator for a new one.",
            "invite_expired": "That invite has expired. Ask the hub's operator for a new one.",
            "no_such_invite": "The hub doesn't know that invite.",
            "needs_invite": "This hub joins by invite only.",
            "instance_too_old": "This hub needs a newer l0wagent release. Update first (./l0w update).",
        }
        msg = friendly.get(code, detail or f"The hub answered {r.status_code}.")
        return Conflict(code, msg) if r.status_code == 409 else BadRequest(code, msg)

    async def _capabilities(self, base: str, pinned: Key | None = None) -> dict:
        r = await self._http("GET", f"{base}/.well-known/l0wagent/hub")
        if r.status_code != 200:
            raise BadRequest("hub_capabilities", f"The hub's capabilities answered {r.status_code}.")
        try:
            doc = r.json()
            return {"doc": doc, "payload": payloads.verify_hub(doc, pinned=pinned)}
        except SignatureError as e:
            if pinned is not None and "pinned" in str(e):
                raise
            raise BadRequest("hub_capabilities", f"The hub's capabilities don't verify: {e}") from e
        except (ProtocolError, ValueError) as e:
            raise BadRequest("hub_capabilities", f"The hub's capabilities don't verify: {e}") from e

    @staticmethod
    def _hub_sign_key(caps: dict) -> Key:
        return next(Key.from_dict(k) for k in caps["keys"] if k.get("use") == "sign" and k.get("alg") == "Ed25519")

    # ── views ──────────────────────────────────────────────────────────────
    async def tunnel_status(self) -> s.TunnelStatus | None:
        r = self.joined()
        if not r or not self.gw:
            return None
        try:
            st = (await self.gw.call("GET", "/v0/tunnel/status")).get("status") or {}
        except GatewayError as e:
            return s.TunnelStatus(state="unknown", error=f"The gateway isn't answering ({e.error}).")
        state = st.get("state") if st.get("state") in ("idle", "connecting", "connected", "offline", "refused") else "unknown"
        acc = st.get("accelerator") if isinstance(st.get("accelerator"), dict) else None  # an older gateway has none: off
        astate = acc.get("state") if acc and acc.get("state") in ("off", "starting", "ready", "active", "error") else "off"
        accelerator = s.AcceleratorStatus(state=astate, since=parse(acc["since"]) if acc and isinstance(acc.get("since"), str) else None,
                                          error=acc.get("error") if acc and isinstance(acc.get("error"), str) else None)
        return s.TunnelStatus(state=state, since=parse(st.get("since")) if isinstance(st.get("since"), str) else None, error=st.get("error"),
                              accelerator=accelerator)

    async def view(self) -> s.Membership:
        r = self.row()
        available = self.gw is not None
        if not r or r["state"] != "joined":
            return s.Membership(state="left" if r else "standalone", available=available, address=r["address"] if r else None,
                                left_at=parse(r["left_at"]) if r else None)
        sign_key = None
        if r["self_doc"]:
            p = json.loads(_b64_payload(r["self_doc"]))
            sk = next((k for k in p.get("keys", []) if k.get("use") == "sign"), None)
            if sk:
                sign_key = s.MemberKey(kid=sk["kid"], created=parse(sk["created"]))  # type: ignore[arg-type]
        chk = r["check_result"]
        return s.Membership(
            state="joined", available=available, address=r["address"],
            hub=s.HubInfo(name=r["hub_name"], domain=r["hub_domain"], base_url=r["hub_base"], policies=(r["capabilities"] or {}).get("policies") or {}),
            hostnames=s.MemberHostnames(**r["hostnames"]),
            urls=s.MemberHostnames(instance=self._origin(r, "instance"), files=self._origin(r, "files")),
            joined_at=parse(r["joined_at"]), tunnel=await self.tunnel_status(),
            accelerator=s.AcceleratorSetting(offered=self.accelerator_api(r) is not None, enabled=bool(r["use_accelerator"])),
            check=s.MembershipCheck(ok=chk["ok"], at=parse(chk["at"]), problems=chk["problems"]) if chk else None,  # type: ignore[arg-type]
            sign_key=sign_key, hub_key_alarm=r["hub_key_alarm"], ask_before_extension_updates=bool(r["ask_before_extension_updates"]),
        )

    # ── joining ────────────────────────────────────────────────────────────
    def _invite(self, invite: str):
        try:
            return parse_invite(invite.strip(), allow_insecure=self.allow_http)
        except ProtocolError as e:
            raise BadRequest("bad_invite", f"That isn't an invite ({e}). It starts with l0w-invite:") from e

    async def preview(self, req: s.InvitePreviewRequest) -> s.InvitePreview:
        inv = self._invite(req.invite)
        caps = (await self._capabilities(inv.base_url))["payload"]
        r = await self._http("GET", f"{caps['api']}invites/{inv.code}")
        if r.status_code != 200:
            raise self._hub_error(r)
        info = r.json()
        problems = []
        if not self.two_factor():
            problems.append("Turn on two-step sign-in (an authenticator app or a passkey) before joining: "
                            "anyone who gets your password could otherwise reach your instance from the internet.")
        if VERSION not in caps.get("protocols", []):
            problems.append(f"This hub doesn't speak {VERSION}.")
        mn, mine = release_tuple(caps["instance_releases"]["min"]), release_tuple(__version__)
        if mn and mine and mine < mn:
            problems.append(f"This hub needs l0wagent {caps['instance_releases']['min']} or newer. Update first.")
        if self.joined():
            problems.append("This instance is already a member of a hub. Leave it first.")
        ex = (caps.get("hostnames") or {}).get("example") or {}
        has_ex = isinstance(ex.get("instance"), str) and isinstance(ex.get("files"), str)
        return s.InvitePreview(
            hub=s.HubInfo(name=caps["name"], domain=caps["domain"], base_url=inv.base_url, policies=caps.get("policies") or {}),
            name_hint=info.get("name_hint"), name_bound=info.get("name_bound") is True and bool(info.get("name_hint")),
            expires=parse(info.get("expires")),
            example=s.MemberHostnames(instance=ex["instance"], files=ex["files"]) if has_ex else None,
            example_name=_example_name(ex) if has_ex else None,
            two_factor_ready=self.two_factor(), problems=problems,
        )

    async def join(self, req: s.JoinRequest) -> s.JoinResult:
        async with self._lock:
            return await self._join(req)

    async def _join(self, req: s.JoinRequest) -> s.JoinResult:
        if self.gw is None:
            raise BadRequest("no_gateway", "This install has no gateway service, so it can't join a hub. Update with ./l0w update.")
        if self.joined():
            raise Conflict("already_joined", "This instance is already a member of a hub. Leave it first.")
        if not self.two_factor():
            raise BadRequest("needs_2fa", "Turn on two-step sign-in (an authenticator app or a passkey) before joining a hub.")
        try:
            name = names.validate_name(req.name.strip().lower())
        except ProtocolError as e:
            raise BadRequest("bad_name", f"{e}. Names are 3–32 lowercase letters and digits, with single dashes between them.") from e
        inv = self._invite(req.invite)
        capsd = await self._capabilities(inv.base_url)
        caps = capsd["payload"]
        hub_key = self._hub_sign_key(caps)
        try:
            reg = await self.gw.call("POST", "/v0/register", {"hub": caps["domain"], "name": name, "invite": inv.code,
                                                              "release": __version__, "protocols": [VERSION]})
        except GatewayError as e:
            raise BadRequest("gateway", f"The gateway couldn't make keys ({e.error}).") from e
        r = await self._http("POST", f"{caps['api']}members", body=reg["registration"])
        if r.status_code != 201:
            raise self._hub_error(r)
        jr = r.json()
        try:
            got_key = Key.from_dict(jr["hub_key"])
            hostnames = {"instance": str(jr["hostnames"]["instance"]).lower(), "files": str(jr["hostnames"]["files"]).lower()}
            address = str(jr["address"]).lower()
        except (KeyError, TypeError, ProtocolError) as e:
            raise BadRequest("bad_join_response", f"The hub's answer didn't make sense: {e}") from e
        if got_key.kid != hub_key.kid or address != payloads.make_address(name, caps["domain"]) or hostnames["instance"] == hostnames["files"]:
            raise BadRequest("bad_join_response", "The hub's answer doesn't match its capabilities (key, address or hostnames).")
        now = self.db.now()
        self.db.ex("DELETE FROM hub_membership WHERE id=1")
        self.db.ex(
            "INSERT INTO hub_membership(id, state, hub_base, hub_domain, hub_name, hub_key, capabilities, api, name, address, mode, hostnames, own_urls,"
            " tunnel_url, self_seq, joined_at, backup) VALUES(1,'joined',?,?,?,?,?,?,?,?,'tunnel',?,NULL,?,-1,?,?)",
            (inv.base_url, caps["domain"], caps["name"], json.dumps(hub_key.to_dict()), json.dumps(caps), caps["api"], name, address,
             json.dumps(hostnames), jr.get("tunnel"), now, req.backup))
        self._log("joined", f"{address} ; backup {req.backup}")
        problems: list[str] = []
        try:
            await self._publish_self()
        except (BadRequest, Conflict, GatewayError) as e:
            problems.append(f"Publishing your self-description failed: {getattr(e, 'detail', '') or e}")
        await self._set_tunnel()
        await self._wait_tunnel(20)
        self._changed()
        if not problems:
            await self.check()
        else:
            self._store_check(problems)
            await self._caps_changed()
        return s.JoinResult(membership=await self.view(), recovery_key=reg["recovery_key"])

    def accelerator_config(self, r: dict) -> dict:
        api = self.accelerator_api(r)
        on = bool(r["use_accelerator"]) and api is not None
        return {"enabled": on, "api": api if on else None}

    async def set_accelerator(self, enabled: bool) -> s.Membership:
        async with self._lock:
            self._need_joined()
            self.db.ex("UPDATE hub_membership SET use_accelerator=? WHERE id=1", (1 if enabled else 0,))
            self._log("accelerator", "on" if enabled else "off")
            await self._set_tunnel()
        self._changed()
        return await self.view()

    async def set_policy(self, ask_before_extension_updates: bool | None) -> s.Membership:
        """Per-hub choices of the owner. `ask_before_extension_updates`: a newer commit of an extension the hub's services installed waits for an OK."""
        async with self._lock:
            self._need_joined()
            if ask_before_extension_updates is not None:
                self.db.ex("UPDATE hub_membership SET ask_before_extension_updates=? WHERE id=1", (1 if ask_before_extension_updates else 0,))
                self._log("extension_updates", "ask first" if ask_before_extension_updates else "apply automatically")
        if self.on_policy:
            self.on_policy()
        self._changed()
        return await self.view()

    async def _set_tunnel(self, off: bool = False) -> None:
        if not self.gw:
            return
        r = self.joined()
        cfg = None
        if r and r["tunnel_url"] and not off:
            cfg = {"url": r["tunnel_url"], "address": r["address"], "hub": r["hub_domain"], "hostnames": r["hostnames"], "release": __version__,
                   "connect_to": self.connect_to, "accelerator": self.accelerator_config(r)}
        try:
            await self.gw.call("PUT", "/v0/tunnel", {"config": cfg})
        except GatewayError as e:
            log.warning("couldn't set the tunnel config: %s", e)

    async def _wait_tunnel(self, seconds: float) -> None:
        for _ in range(int(seconds * 4)):
            st = await self.tunnel_status()
            if st is None or st.state == "connected":
                return
            await asyncio.sleep(0.25)

    async def _publish_self(self, *, retry_seq: bool = True) -> dict:
        """Sign the next self-description at the gateway and push it to the hub; store it once the hub accepts it."""
        r = self.joined()
        assert r is not None
        seq = int(r["self_seq"]) + 1
        doc = (await self.gw.call("POST", "/v0/sign/self", {"address": r["address"], "seq": seq, "hostnames": r["hostnames"],  # type: ignore[union-attr]
                                                            "protocols": [VERSION]}))["doc"]
        resp = await self._http("PUT", f"{r['api']}members/{r['name']}/self", body=doc)
        if resp.status_code != 200:
            raise self._hub_error(resp)
        try:
            payloads.verify_self(doc)
            docs.verify_directory_entry(resp.json(), Key.from_dict(r["hub_key"]), hub_domain=r["hub_domain"], name=r["name"])
        except (ProtocolError, ValueError) as e:
            raise BadRequest("bad_directory_entry", f"The hub's countersigned entry doesn't verify: {e}") from e
        self.db.ex("UPDATE hub_membership SET self_doc=?, self_seq=? WHERE id=1", (json.dumps(doc), seq))
        return doc

    # ── the self-check ─────────────────────────────────────────────────────
    def _store_check(self, problems: list[str]) -> s.MembershipCheck:
        prev = (self.row() or {}).get("check_result") or {}
        at = iso(self.now())
        self.db.ex("UPDATE hub_membership SET check_at=?, check_result=? WHERE id=1", (at, json.dumps({"ok": not problems, "at": at, "problems": problems})))
        if problems and prev.get("ok", True):
            self.notifier.notify("hub", "Hub check failed", problems[0], "/settings/hub")
        self._log("check", "ok" if not problems else "; ".join(problems))
        self._changed()
        return s.MembershipCheck(ok=not problems, at=parse(at), problems=problems)  # type: ignore[arg-type]

    async def check(self) -> s.MembershipCheck:
        """The self-check: the hub's capabilities still carry the pinned key; the directory serves our current
        self-description; both hostnames answer from outside (the instance one with our self-description, the
        files one from the files app, recognisable by its sandbox header)."""
        r = self.joined()
        if not r:
            raise Conflict("not_joined", "This instance isn't a member of a hub.")
        problems: list[str] = []
        caps_ok = False
        pinned = Key.from_dict(r["hub_key"])
        try:
            caps = (await self._capabilities(r["hub_base"], pinned=pinned))["payload"]
            self.db.ex("UPDATE hub_membership SET capabilities=?, hub_key_alarm=NULL WHERE id=1", (json.dumps(caps),))
            if self.accelerator_api({**r, "capabilities": caps}) != self.accelerator_api(r):
                await self._set_tunnel()  # the hub began or stopped offering the faster connection
            caps_ok = True
        except SignatureError:
            alarm = "The hub's signing key changed with no rotation from the key you joined with. Don't trust it until you've checked with the hub's operator."
            if not r["hub_key_alarm"]:
                self.notifier.notify("hub", "Your hub's key changed", alarm, "/settings/hub")
            self.db.ex("UPDATE hub_membership SET hub_key_alarm=? WHERE id=1", (alarm,))
            problems.append(alarm)
        except BadRequest as e:
            problems.append(f"The hub's capabilities: {e.detail}")
        mine = (r["self_doc"] or {}).get("payload")
        if not mine:
            try:
                mine = (await self._publish_self())["payload"]
            except (BadRequest, Conflict, GatewayError) as e:
                problems.append(f"Publishing your self-description failed: {getattr(e, 'detail', '') or e}")
        if mine:
            entry_payload = await self._directory_payload(r, pinned, problems)
            if entry_payload is not None and entry_payload != mine:
                try:
                    mine = (await self._publish_self())["payload"]
                except (BadRequest, Conflict, GatewayError) as e:
                    problems.append(f"The hub's directory has an old copy of your self-description, and pushing a new one failed: {getattr(e, 'detail', '') or e}")
        try:
            inst = await self._http("GET", f"{self._origin(r, 'instance')}/.well-known/l0wagent/self", headers={"Accept": "application/json"})
            if inst.status_code != 200 or (mine and inst.json().get("payload") != mine):
                problems.append(f"Your instance hostname ({r['hostnames']['instance']}) doesn't serve your self-description (it answered {inst.status_code}).")
        except (BadRequest, ValueError) as e:
            problems.append(f"Your instance hostname doesn't answer: {getattr(e, 'detail', '') or e}")
        try:
            files = await self._http("GET", f"{self._origin(r, 'files')}/")
            if "sandbox" not in files.headers.get("content-security-policy", ""):
                problems.append(f"Your files hostname ({r['hostnames']['files']}) doesn't reach the files app (it answered {files.status_code}).")
        except BadRequest as e:
            problems.append(f"Your files hostname doesn't answer: {e.detail}")
        result = self._store_check(problems)
        if caps_ok:
            await self._caps_changed()
        return result

    async def _caps_changed(self) -> None:
        """The hub's capabilities were read: its services follow them."""
        if self.on_caps:
            await self.on_caps()

    async def _directory_payload(self, r: dict, hub_key: Key, problems: list[str]) -> str | None:
        try:
            resp = await self._http("GET", f"{r['hub_base']}/.well-known/l0wagent/members/{r['name']}")
        except BadRequest as e:
            problems.append(f"The hub's directory: {e.detail}")
            return None
        if resp.status_code == 404:
            return ""
        if resp.status_code != 200:
            problems.append(f"The hub's directory answered {resp.status_code} for your name.")
            return None
        try:
            entry = resp.json()
            docs.verify_directory_entry(entry, hub_key, hub_domain=r["hub_domain"], name=r["name"])
            return entry["payload"]
        except (ProtocolError, ValueError, KeyError) as e:
            problems.append(f"The hub's directory entry for you doesn't verify: {e}")
            return None

    async def housekeeping(self, now: datetime) -> None:
        r = self.joined()
        if not r or not self.gw:
            return
        last = parse(r["check_at"])
        if last is None or now - last >= CHECK_EVERY:
            await self.check()

    # ── keys ───────────────────────────────────────────────────────────────
    async def rotate(self) -> s.Membership:
        async with self._lock:
            r = self._need_joined()
            await self._gw("POST", "/v0/rotate", {"address": r["address"]})
            self._log("rotated", "scheduled")
            await self._publish_self()
            self._changed()
            return await self.view()

    async def recover(self, req: s.RecoverRequest) -> s.Membership:
        async with self._lock:
            r = self._need_joined()
            try:
                await self.gw.call("POST", "/v0/recover", {"address": r["address"], "recovery_key": req.recovery_key.strip()})  # type: ignore[union-attr]
            except GatewayError as e:
                if e.error == "wrong_recovery_key":
                    raise BadRequest("wrong_recovery_key", "That isn't this instance's recovery key.") from e
                raise BadRequest("gateway", f"The gateway refused ({e.error}).") from e
            self._log("rotated", "recovery key")
            await self._publish_self()
            self._changed()
            return await self.view()

    async def leave(self, req: s.LeaveRequest) -> s.Membership:
        async with self._lock:
            r = self._need_joined()
            if req.confirm.strip().lower() != r["address"]:
                raise BadRequest("confirm", f"Type your address ({r['address']}) to confirm.")
            doc = (await self._gw("POST", "/v0/sign/leave", {"address": r["address"], "moved_to": None}))["doc"]
            resp = await self._http("POST", f"{r['api']}members/{r['name']}/leave", body=doc)
            if resp.status_code not in (200, 410):
                raise self._hub_error(resp)
            await self._set_tunnel(off=True)
            self.db.ex("UPDATE hub_membership SET state='left', left_at=? WHERE id=1", (self.db.now(),))
            self._log("left", r["address"])
            if self.on_left:
                await self.on_left()
            self._changed()
            return await self.view()

    def _need_joined(self) -> dict:
        r = self.joined()
        if not r or not self.gw:
            raise Conflict("not_joined", "This instance isn't a member of a hub.")
        return r

    async def _gw(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            return await self.gw.call(method, path, body)  # type: ignore[union-attr]
        except GatewayError as e:
            raise BadRequest("gateway", f"The gateway refused ({e.error}).") from e

    async def startup(self) -> None:
        """Re-send the tunnel's settings (the gateway keeps them, but a restored gateway may not)."""
        if self.joined() and self.gw:
            await self._set_tunnel()


def _b64_payload(doc: dict) -> str:
    from l0wproto import b64

    return b64.decode(doc["payload"]).decode()


def hub_origins(request) -> tuple[str | None, str | None]:
    """(instance origin, files origin) on the hub, when this request came through it: from the tunnel's
    network, or addressed to the instance hostname the hub assigned. (None, None) otherwise."""
    from .files_origin import host_of
    from .netaddr import via_tunnel

    be = request.app.state.backend
    if not hasattr(be, "public_origins"):
        return None, None
    inst, files = be.public_origins()
    if not inst:
        return None, None
    host = (request.headers.get("host") or "").lower()
    if via_tunnel(request) or host_of(f"{urlsplit(inst).scheme}://{host}") == host_of(inst):
        return inst, files
    return None, None
