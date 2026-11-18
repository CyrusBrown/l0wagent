"""The gateway's HTTP API (see README.md). Two bearer tokens: control's and the tunnel's."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from l0wproto import b64, docs, hopsig, keys, payloads
from l0wproto.errors import ProtocolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .store import Store

RECOVERY_PREFIX = "l0w-recovery:"
STATES = ("idle", "connecting", "connected", "offline", "refused")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        self.status, self.code, self.detail = status, code, detail


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


def _keypair(entry: dict, use: str) -> keys.KeyPair:
    kp = keys.from_seed(b64.decode(entry["seed"]), use, entry["pub"]["created"])
    if kp.kid != entry["pub"]["kid"]:
        raise ApiError(500, "corrupt_keys", "stored key does not match its public half")
    return kp


def _pub(state: dict) -> list[dict]:
    return [state["sign"]["pub"], state["seal"]["pub"], state["recover"]["pub"]]


def _entry(kp: keys.KeyPair) -> dict:
    return {"seed": b64.encode(kp.private), "pub": kp.key.to_dict()}


def create_app(data_dir, control_token: str, tunnel_token: str) -> Starlette:
    if not control_token or not tunnel_token:
        raise ValueError("GATEWAY_CONTROL_TOKEN and GATEWAY_TUNNEL_TOKEN are both required")
    if secrets.compare_digest(control_token.encode(), tunnel_token.encode()):
        raise ValueError("GATEWAY_CONTROL_TOKEN and GATEWAY_TUNNEL_TOKEN must differ")
    store = Store(data_dir)

    def bearer(request: Request) -> bytes:
        h = request.headers.get("authorization", "")
        return h[7:].encode() if h[:7].lower() == "bearer " else b""

    def endpoint(who: str, fn: Callable[[Request, dict], dict], method_has_body: bool = True):
        token = (control_token if who == "control" else tunnel_token).encode()

        async def handler(request: Request) -> JSONResponse:
            try:
                got = bearer(request)
                if not got or not secrets.compare_digest(got, token):
                    raise ApiError(401, "unauthorized")
                body: dict = {}
                if method_has_body:
                    raw = await request.body()
                    try:
                        body = await request.json() if raw else {}
                    except ValueError as e:
                        raise bad("body is not valid JSON") from e
                    if not isinstance(body, dict):
                        raise bad("body must be a JSON object")
                with store.lock:
                    return JSONResponse(fn(request, body))
            except ApiError as e:
                return JSONResponse({"error": e.code, "detail": e.detail}, e.status)
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
        mode = b.get("mode")
        if mode not in ("tunnel", "own-url"):
            raise bad("mode must be tunnel or own-url")
        own = _url_pair(b.get("own_urls"), "own_urls", null=True)
        release = _str(b, "release")
        protocols = b.get("protocols")
        if not isinstance(protocols, list) or not all(isinstance(p, str) for p in protocols):
            raise bad("protocols must be a list of strings")
        sign, seal, rec = keys.generate("sign"), keys.generate("seal"), keys.generate("recover")
        payload = payloads.build_registration(hub, name, invite, mode, [sign.key, seal.key, rec.key], own_urls=own, release=release, protocols=protocols)
        doc = docs.sign_document("registration", payload, [sign, rec])
        old = store.keys()
        retired = (old["retired"] if old else []) + ([old["sign"]["pub"], old["seal"]["pub"], old["recover"]["pub"]] if old else [])
        store.save_keys({"sign": _entry(sign), "seal": _entry(seal), "recover": {"pub": rec.key.to_dict()}, "retired": retired, "history": []})
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
        extra = {}
        if b.get("own_urls") is not None:
            extra["own_urls"] = _url_pair(b["own_urls"], "own_urls", null=False)
        payload = payloads.build_self(_str(b, "address"), seq, _pub(st), hostnames, protocols=protocols, history=st["history"], **extra)
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
        store.save_status({k: b.get(k) for k in ("state", "since", "error", "session", "close_code")})
        return {"ok": True}

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def r(path: str, method: str, who: str, fn, body: bool = True) -> Route:
        return Route(path, endpoint(who, fn, body), methods=[method])

    return Starlette(
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
            r("/v0/tunnel/status", "PUT", "tunnel", tunnel_status_put),
        ]
    )
