"""The gateway's HTTP API (see README.md). Two bearer tokens: control's and the tunnel's."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import secrets
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from l0wproto import b64, docs, hopsig, keys, payloads, spaces
from l0wproto.consts import MAX_ENVELOPE
from l0wproto.errors import ProtocolError, SealError
from l0wproto.times import format_time
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .peers import BACKOFF_FIRST, RETIRED_SEAL_KEEP, ApiError, Net, Peers
from .store import Store

RECOVERY_PREFIX = "l0w-recovery:"
STATES = ("idle", "connecting", "connected", "offline", "refused")
ACCEL_STATES = ("off", "starting", "ready", "active", "error")
ACCEL_ACTIONS = ("enable", "activate", "deactivate", "disable")


def bad(detail: str) -> ApiError:
    return ApiError(400, "bad_request", detail)


def _str(body: dict, field: str, *, empty: bool = False) -> str:
    v = body.get(field)
    if not isinstance(v, str) or (not v and not empty):
        raise bad(f"{field} must be a non-empty string")
    return v


def _url_pair(v: object, field: str, *, null: bool) -> dict | None:
    if v is None and null:
        return None
    if not isinstance(v, dict) or not all(isinstance(v.get(k), str) and v[k] for k in ("instance", "files")):
        raise bad(f"{field} must have string instance and files")
    return {"instance": v["instance"], "files": v["files"]}


def _accelerator(v: object) -> dict:
    if v is None:
        return {"enabled": False, "api": None}
    if not isinstance(v, dict):
        raise bad("accelerator must be an object")
    enabled, api = v.get("enabled", False), v.get("api")
    if not isinstance(enabled, bool):
        raise bad("accelerator.enabled must be a boolean")
    if api is not None:
        if not isinstance(api, str) or urlsplit(api).scheme not in ("http", "https") or not urlsplit(api).netloc:
            raise bad("accelerator.api must be an http(s) URL or null")
    return {"enabled": enabled, "api": api}


def _keypair(entry: dict, use: str) -> keys.KeyPair:
    kp = keys.from_seed(b64.decode(entry["seed"]), use, entry["pub"]["created"])
    if kp.kid != entry["pub"]["kid"]:
        raise ApiError(500, "corrupt_keys", "stored key does not match its public half")
    return kp


def peers_age(retired: dict, now: int) -> int:
    from l0wproto.times import parse_time

    return now - parse_time(retired["retired_at"])


def _pub(state: dict) -> list[dict]:
    return [state["sign"]["pub"], state["seal"]["pub"], state["recover"]["pub"]]


def _entry(kp: keys.KeyPair) -> dict:
    return {"seed": b64.encode(kp.private), "pub": kp.key.to_dict()}


def create_app(data_dir, control_token: str, tunnel_token: str, *, net: Net | None = None, clock: Callable[[], float] = time.time,
               outbox_interval: float = 5.0, backoff_first: int = BACKOFF_FIRST) -> Starlette:
    if not control_token or not tunnel_token:
        raise ValueError("GATEWAY_CONTROL_TOKEN and GATEWAY_TUNNEL_TOKEN are both required")
    if secrets.compare_digest(control_token.encode(), tunnel_token.encode()):
        raise ValueError("GATEWAY_CONTROL_TOKEN and GATEWAY_TUNNEL_TOKEN must differ")
    store = Store(data_dir)
    peers = Peers(store, net or Net(), clock, backoff_first)
    last_accel_iat = [0]  # the last accelerator-request iat this gateway signed (epoch seconds)

    def bearer(request: Request) -> bytes:
        h = request.headers.get("authorization", "")
        return h[7:].encode() if h[:7].lower() == "bearer " else b""

    def endpoint(who: str, fn: Callable, method_has_body: bool = True, raw_body: bool = False):
        """`fn(request, body)` is a plain function (run with the store lock held) or a coroutine function (it takes the lock
        itself around its state changes, never across a network call). With `raw_body` the body is the request's bytes, at most 16 MiB."""
        token = (control_token if who == "control" else tunnel_token).encode()

        async def handler(request: Request) -> JSONResponse:
            try:
                got = bearer(request)
                if not got or not secrets.compare_digest(got, token):
                    raise ApiError(401, "unauthorized")
                body: dict | bytes = {}
                if raw_body:
                    if int(request.headers.get("content-length") or 0) > MAX_ENVELOPE:
                        raise ApiError(413, "too_large", "the envelope is over 16 MiB")
                    buf = bytearray()
                    async for chunk in request.stream():
                        buf += chunk
                        if len(buf) > MAX_ENVELOPE:
                            raise ApiError(413, "too_large", "the envelope is over 16 MiB")
                    body = bytes(buf)
                elif method_has_body:
                    raw = await request.body()
                    try:
                        body = await request.json() if raw else {}
                    except ValueError as e:
                        raise bad("body is not valid JSON") from e
                    if not isinstance(body, dict):
                        raise bad("body must be a JSON object")
                if inspect.iscoroutinefunction(fn):
                    return JSONResponse(await fn(request, body))
                with store.lock:
                    return JSONResponse(fn(request, body))
            except ApiError as e:
                return JSONResponse({"error": e.code, "detail": e.detail, **e.extra}, e.status)
            except ProtocolError as e:
                return JSONResponse({"error": "bad_request", "detail": e.detail}, 400)

        return handler

    def need_keys() -> dict:
        st = store.keys()
        if not st:
            raise ApiError(409, "no_keys", "no keys yet: register first")
        return st

    # --- control token ---

    def get_keys(_r, _b):
        st = store.keys()
        return {"keys": _pub(st) if st else [], "history": st["history"] if st else []}

    def register(_r, b):
        hub, name, invite = _str(b, "hub"), _str(b, "name"), _str(b, "invite")
        release = _str(b, "release")
        protocols = b.get("protocols")
        if not isinstance(protocols, list) or not all(isinstance(p, str) for p in protocols):
            raise bad("protocols must be a list of strings")
        sign, seal, rec = keys.generate("sign"), keys.generate("seal"), keys.generate("recover")
        payload = payloads.build_registration(hub, name, invite, [sign.key, seal.key, rec.key], release=release, protocols=protocols)
        doc = docs.sign_document("registration", payload, [sign, rec])
        old = store.keys()
        retired = (old["retired"] if old else []) + ([old["sign"]["pub"], old["seal"]["pub"], old["recover"]["pub"]] if old else [])
        # the encryption keys we replace stay usable for 8 days, so envelopes sealed to them still open (peers.seal_seeds)
        now = int(clock())
        retired_seal = [x for x in (old.get("retired_seal", []) if old else []) if peers_age(x, now) < RETIRED_SEAL_KEEP]
        if old:
            retired_seal.append({"seed": old["seal"]["seed"], "pub": old["seal"]["pub"], "retired_at": format_time(now)})
        store.save_keys({"sign": _entry(sign), "seal": _entry(seal), "recover": {"pub": rec.key.to_dict()}, "retired": retired, "retired_seal": retired_seal,
                         "history": []})
        return {"registration": doc, "keys": [k.key.to_dict() for k in (sign, seal, rec)], "recovery_key": RECOVERY_PREFIX + b64.encode(rec.private)}

    def sign_self(_r, b):
        st = need_keys()
        seq = b.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise bad("seq must be a non-negative integer")
        hostnames = _url_pair(b.get("hostnames"), "hostnames", null=False)
        protocols = b.get("protocols")
        if not isinstance(protocols, list) or not all(isinstance(p, str) for p in protocols):
            raise bad("protocols must be a list of strings")
        payload = payloads.build_self(_str(b, "address"), seq, _pub(st), hostnames, protocols=protocols, history=st["history"])
        return {"doc": docs.sign_document("self", payload, [_keypair(st["sign"], "sign")])}

    def sign_leave(_r, b):
        st = need_keys()
        moved = b.get("moved_to")
        if moved is not None and not isinstance(moved, str):
            raise bad("moved_to must be a string or null")
        payload = payloads.build_leave(_str(b, "address"), moved)
        return {"doc": docs.sign_document("leave", payload, [_keypair(st["sign"], "sign")])}

    def sign_moved(_r, b):
        st = need_keys()
        payload = payloads.build_moved(_str(b, "from"), _str(b, "to"))
        return {"doc": docs.sign_document("moved", payload, [_keypair(st["sign"], "sign")])}

    def rotate_with(st: dict, address: str, signer: keys.KeyPair, reason: str) -> dict:
        old_pub = st["sign"]["pub"]
        new = keys.generate("sign")
        doc = docs.sign_document("rotation", payloads.build_rotation(address, old_pub["kid"], new.key, reason), [signer])
        st["retired"].append(old_pub)
        st["history"].append(doc)
        st["sign"] = _entry(new)
        store.save_keys(st)
        return {"rotation": doc, "keys": _pub(st)}

    def rotate(_r, b):
        st = need_keys()
        address = _str(b, "address")
        return rotate_with(st, address, _keypair(st["sign"], "sign"), "scheduled")

    def recover(_r, b):
        st = need_keys()
        address, rk = _str(b, "address"), _str(b, "recovery_key")
        try:
            if not rk.startswith(RECOVERY_PREFIX):
                raise ValueError
            seed = b64.decode(rk[len(RECOVERY_PREFIX):])
            kp = keys.from_seed(seed, "recover", st["recover"]["pub"]["created"])
        except (ValueError, ProtocolError):
            raise ApiError(403, "wrong_recovery_key", "that is not a recovery key") from None
        if not secrets.compare_digest(kp.kid.encode(), st["recover"]["pub"]["kid"].encode()):
            raise ApiError(403, "wrong_recovery_key", "that recovery key does not belong to this instance")
        return rotate_with(st, address, kp, "compromise")

    def hop_sign(_r, b):
        st = need_keys()
        method, url, address = _str(b, "method"), _str(b, "url"), _str(b, "address")
        raw = b.get("body", "")
        if not isinstance(raw, str):
            raise bad("body must be a base64url string")
        ctype = b.get("content_type")
        if ctype is not None and not isinstance(ctype, str):
            raise bad("content_type must be a string or null")
        data = b64.decode(raw)
        if data and not ctype:
            raise bad("a request with a body needs content_type")
        sr = hopsig.sign_request(method, url, data, ctype, _keypair(st["sign"], "sign"), address)
        return {"headers": sr.headers}

    def put_tunnel(_r, b):
        if "config" not in b:
            raise bad("config is required (null clears it)")
        c = b["config"]
        if c is not None:
            if not isinstance(c, dict):
                raise bad("config must be an object or null")
            ct = c.get("connect_to")
            if ct is not None and not isinstance(ct, str):
                raise bad("connect_to must be a string or null")
            c = {
                "url": _str(c, "url"),
                "address": _str(c, "address"),
                "hub": _str(c, "hub"),
                "hostnames": _url_pair(c.get("hostnames"), "hostnames", null=False),
                "release": _str(c, "release"),
                "connect_to": ct,
                "accelerator": _accelerator(c.get("accelerator")),
            }
        store.save_tunnel(c)
        if c is None:
            store.save_status(None)
        return {"config": c}

    def tunnel_status_get(_r, _b):
        return {"config": store.tunnel() is not None, "status": store.status()}

    # --- tunnel token ---

    def tunnel_config(_r, _b):
        return {"config": store.tunnel()}

    def tunnel_hello(_r, b):
        cfg = store.tunnel()
        if cfg is None:
            raise ApiError(409, "no_tunnel", "no tunnel configured")
        nonce, hub = _str(b, "nonce"), _str(b, "hub")
        if hub.lower() != cfg["hub"].lower():
            raise ApiError(403, "wrong_hub", "the hello is for another hub")
        st = need_keys()
        payload = payloads.build_tunnel_hello(cfg["address"], nonce, hub, cfg["release"])
        return {"doc": docs.sign_document("tunnel-hello", payload, [_keypair(st["sign"], "sign")])}

    def tunnel_accelerator(_r, b):
        cfg = store.tunnel()
        if cfg is None:
            raise ApiError(409, "no_tunnel", "no tunnel configured")
        action = b.get("action")
        if action not in ACCEL_ACTIONS:
            raise bad(f"action must be one of {', '.join(ACCEL_ACTIONS)}")
        service = b.get("service")
        if action == "enable":
            if not payloads.valid_service(service):
                raise bad("service must be http://127.0.0.1:<port>")
        else:
            service = None
        iat = b.get("iat")
        if iat is not None and (not isinstance(iat, int) or isinstance(iat, bool)):
            raise bad("iat must be an integer (epoch seconds) or absent")
        st = need_keys()
        # strictly later than the last one signed here, so rapid calls are never `stale`
        when = max(int(time.time()) if iat is None else iat, last_accel_iat[0] + 1)
        last_accel_iat[0] = when
        kw = {"service": service} if service else {}
        payload = payloads.build_accelerator_request(cfg["address"], cfg["hub"], action, iat=when, **kw)
        return {"doc": docs.sign_document("accelerator-request", payload, [_keypair(st["sign"], "sign")])}

    def tunnel_status_put(_r, b):
        state = b.get("state")
        if state not in STATES:
            raise bad(f"state must be one of {', '.join(STATES)}")
        for f in ("since", "error"):
            if b.get(f) is not None and not isinstance(b[f], str):
                raise bad(f"{f} must be a string or null")
        for f in ("session", "close_code"):
            if b.get(f) is not None and (not isinstance(b[f], (str, int)) or isinstance(b[f], bool)):
                raise bad(f"{f} must be a string, integer or null")
        saved = {k: b.get(k) for k in ("state", "since", "error", "session", "close_code")}
        a = b.get("accelerator")
        if a is not None:
            if not isinstance(a, dict) or a.get("state") not in ACCEL_STATES:
                raise bad(f"accelerator.state must be one of {', '.join(ACCEL_STATES)}")
            for f in ("since", "error"):
                if a.get(f) is not None and not isinstance(a[f], str):
                    raise bad(f"accelerator.{f} must be a string or null")
            saved["accelerator"] = {"state": a["state"], "since": a.get("since"), "error": a.get("error")}
        else:  # a report that says nothing about the accelerator leaves its last state alone
            prev = (store.status() or {}).get("accelerator")
            if prev:
                saved["accelerator"] = prev
        store.save_status(saved)
        return {"ok": True}

    # --- friends and messages (control token) ---

    def peer_fingerprint(_r, _b):
        need_keys()
        return {"fingerprint": peers.fingerprint()}

    async def peer_pin(_r, b):
        fp, held = b.get("fingerprint"), b.get("held_token")
        if fp is not None and not isinstance(fp, str):
            raise bad("fingerprint must be a string")
        if held is not None and not isinstance(held, dict):
            raise bad("held_token must be a token document")
        return await peers.pin(_str(b, "address"), fp, held)

    def peer_token(_r, b):
        return peers.issue_token(_str(b, "address"))

    def peer_revoke(_r, b):
        return peers.revoke(_str(b, "address"))

    def peer_forget(_r, b):
        return peers.forget(_str(b, "address"))

    async def peer_send(_r, b):
        rt = b.get("request_token", False)
        if not isinstance(rt, bool):
            raise bad("request_token must be a boolean")
        return await peers.send(_str(b, "to"), _str(b, "kind"), b.get("body"), rt)

    def peer_services(_r, b):
        return peers.set_services(b.get("addresses"))

    def peer_outbox(r, _b):
        try:
            return {"entries": peers.outbox_view(r.query_params.get("since"))}
        except ProtocolError:
            raise bad("since must be a time like 2026-11-22T10:00:00Z") from None

    async def peer_open(_r, raw):
        return await peers.open(raw)

    # --- shared spaces (spaces.md; control token) ---

    def space_open_key(_r, b):
        """Open a space-key document sealed to one of our seal keys. The `info` is built from the document's own fields,
        so this opens nothing else. The granter's signature is not checked here: control does that."""
        doc = b.get("doc")
        if not isinstance(doc, dict):
            raise bad("doc must be a space-key document")
        try:
            p = spaces.space_key_payload(doc, peers.now())
        except ProtocolError as e:
            raise bad(f"not a space-key document: {e.detail}") from None
        own = peers.own_address()
        if payloads.split_address(p["member"]) != payloads.split_address(own):
            raise ApiError(403, "not_for_us", "the key is sealed to another member")
        seed = peers.seal_seeds().get(p["member_kid"])
        if seed is None:
            raise ApiError(403, "not_for_us", "the key is sealed to a key this instance does not hold")
        try:
            k = spaces.open_space_key_payload(seed, p)
        except SealError:
            raise ApiError(422, "cannot_open", "the key does not open") from None
        return {"key": b64.encode(k)}

    SPACE_VERSION_FIELDS = {"v", "type", "iat", "space", "seq", "parent", "epoch", "author", "blobs", "meta"}

    def sign_space_version(_r, b):
        """Sign a space-version document as this instance. Only the spec's fields, and only with this instance as author."""
        p = b.get("payload")
        if not isinstance(p, dict):
            raise bad("payload must be a space-version payload object")
        p = {"v": p.get("v", "l0w/0.1"), "type": p.get("type", "space-version"), "iat": p.get("iat", format_time(peers.now())),
             **{k: x for k, x in p.items() if k not in ("v", "type", "iat")}}
        if set(p) - SPACE_VERSION_FIELDS:
            raise bad("a space-version payload has only the spec's fields")
        try:
            spaces.validate_space_version(p)
        except ProtocolError as e:
            raise bad(f"not a space-version payload: {e.detail}") from None
        if len(docs.serialize_payload(p)) > payloads.MAX_SERVICE_BODY:
            raise ApiError(413, "too_large", "the version is over 1 MiB")
        own = peers.own_address()
        if payloads.split_address(p["author"]) != payloads.split_address(own):
            raise ApiError(403, "not_the_author", "this instance signs only its own versions")
        return {"doc": docs.sign_document("space-version", p, [peers.sign_pair()])}

    SPACE_KEY_FIELDS = {"v", "type", "iat", "space", "epoch", "member", "member_kid", "granter", "granter_kid", "sealed"}

    def sign_space_key(_r, b):
        """Sign a space-key document as this instance (the space's creator grants a member a key it sealed itself). Only the spec's
        fields, only with this instance as granter and its current signing key as granter_kid. The gateway never sees the key."""
        p = b.get("payload")
        if not isinstance(p, dict):
            raise bad("payload must be a space-key payload object")
        p = {"v": p.get("v", "l0w/0.1"), "type": p.get("type", "space-key"), "iat": p.get("iat", format_time(peers.now())),
             **{k: x for k, x in p.items() if k not in ("v", "type", "iat")}}
        if set(p) - SPACE_KEY_FIELDS:
            raise bad("a space-key payload has only the spec's fields")
        try:
            spaces.validate_space_key(p)
        except ProtocolError as e:
            raise bad(f"not a space-key payload: {e.detail}") from None
        pair = peers.sign_pair()
        if payloads.split_address(p["granter"]) != payloads.split_address(peers.own_address()) or p["granter_kid"] != pair.key.kid:
            raise ApiError(403, "not_the_granter", "this instance signs only keys it grants, with its current signing key")
        return {"doc": docs.sign_document("space-key", p, [pair])}

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def r(path: str, method: str, who: str, fn, body: bool = True) -> Route:
        return Route(path, endpoint(who, fn, body), methods=[method])

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(peers.run(outbox_interval))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            r("/v0/keys", "GET", "control", get_keys, False),
            r("/v0/register", "POST", "control", register),
            r("/v0/sign/self", "POST", "control", sign_self),
            r("/v0/sign/leave", "POST", "control", sign_leave),
            r("/v0/sign/moved", "POST", "control", sign_moved),
            r("/v0/rotate", "POST", "control", rotate),
            r("/v0/recover", "POST", "control", recover),
            r("/v0/hop-sign", "POST", "control", hop_sign),
            r("/v0/tunnel", "PUT", "control", put_tunnel),
            r("/v0/tunnel/status", "GET", "control", tunnel_status_get, False),
            r("/v0/tunnel/config", "GET", "tunnel", tunnel_config, False),
            r("/v0/tunnel/hello", "POST", "tunnel", tunnel_hello),
            r("/v0/tunnel/accelerator", "POST", "tunnel", tunnel_accelerator),
            r("/v0/tunnel/status", "PUT", "tunnel", tunnel_status_put),
            r("/v0/peer/fingerprint", "GET", "control", peer_fingerprint, False),
            r("/v0/peer/pin", "POST", "control", peer_pin),
            r("/v0/peer/token", "POST", "control", peer_token),
            r("/v0/peer/revoke", "POST", "control", peer_revoke),
            r("/v0/peer/forget", "POST", "control", peer_forget),
            r("/v0/peer/send", "POST", "control", peer_send),
            r("/v0/peer/services", "PUT", "control", peer_services),
            r("/v0/space/open-key", "POST", "control", space_open_key),
            r("/v0/sign/space-version", "POST", "control", sign_space_version),
            r("/v0/sign/space-key", "POST", "control", sign_space_key),
            r("/v0/peer/outbox", "GET", "control", peer_outbox, False),
            Route("/v0/peer/open", endpoint("control", peer_open, raw_body=True), methods=["POST"]),
        ],
    )
    app.state.peers = peers
    return app
