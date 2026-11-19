"""The hub's HTTP API: capabilities, invites, membership, the signed directory. The tunnel and the
host-routing front are in tunnel.py."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from l0wproto import VERSION, b64, docs, payloads
from l0wproto.consts import SUPPORTED_VERSIONS
from l0wproto.errors import DocumentError, NameRuleError, SignatureError, UnsupportedVersion
from l0wproto.keys import Key
from l0wproto.names import check_name
from l0wproto.times import check_fresh, format_time, now, parse_time
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Config
from .store import Store, load_hub_key
from .tunnel import TunnelServer

USES = ("sign", "seal", "recover")


@dataclass
class HubError(Exception):
    status: int
    code: str
    detail: str = ""
    extra: dict | None = None
    headers: dict | None = None


def release_tuple(s: str) -> tuple:
    try:
        return tuple(int(x) for x in str(s).split("-")[0].split("+")[0].split("."))
    except ValueError:
        return (0,)


def origin(url: str) -> tuple:
    u = urlsplit(url)
    return (u.scheme, (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80))


class Hub:
    def __init__(self, cfg: Config, *, http_client_factory=None):
        self.cfg = cfg
        self.store = Store(cfg.state_path)
        self.key = load_hub_key(cfg.seed_path)
        self.tunnels = TunnelServer(self)
        #: member name -> {tunnel_id, token, active, last_iat}; in memory only (the fake has no Cloudflare)
        self.cloudflared: dict[str, dict] = {}
        self.cloudflared_iat: dict[str, int] = {}  # member name -> last accepted iat
        self._http_factory = http_client_factory or (
            lambda: httpx.AsyncClient(verify=cfg.own_url_verify_tls, timeout=5.0, follow_redirects=False))

    # --- helpers --------------------------------------------------------------------------

    def error(self, e: HubError) -> JSONResponse:
        body = {"v": VERSION, "error": e.code, "detail": e.detail, **(e.extra or {})}
        return JSONResponse(body, status_code=e.status, headers=e.headers)

    def address(self, name: str) -> str:
        return f"{name}@{self.cfg.domain}"

    def host_map(self) -> dict[str, tuple[str, str]]:
        """Hostnames of active tunnel-mode members -> (name, "instance" | "files")."""
        out = {}
        with self.store.read() as s:
            for name, m in s["members"].items():
                if m["status"] == "active" and m["mode"] == "tunnel":
                    out[m["hostnames"]["instance"].lower()] = (name, "instance")
                    out[m["hostnames"]["files"].lower()] = (name, "files")
        return out

    async def body(self, request: Request, code: str):
        try:
            doc = json.loads(await request.body())
        except ValueError as e:
            raise HubError(400, code, "body is not JSON") from e
        if not isinstance(doc, dict):
            raise HubError(400, code, "body must be a JSON object")
        return doc

    def _member_or_raise(self, s: dict, name: str) -> dict:
        m = s["members"].get(name)
        if m is None:
            raise HubError(404, "no_such_member", "no such member")
        if m["status"] == "left":
            raise HubError(410, "left", "this member has left")
        return m

    def parse_signed(self, fn, code: str, *args, **kw):
        try:
            return fn(*args, **kw)
        except UnsupportedVersion as e:
            raise HubError(400, "unsupported_version", str(e), {"supported": list(SUPPORTED_VERSIONS)}) from e
        except Exception as e:  # ProtocolError, or anything odd in a hostile document
            raise HubError(400, code, str(e) or type(e).__name__) from e

    # --- capabilities ---------------------------------------------------------------------

    async def capabilities(self, request: Request):
        c = self.cfg
        payload = payloads.build_hub(
            c.name, c.domain, [self.key.key], c.api, c.tunnel_url, min_release=c.min_release,
            membership={"invites": True, "signups": "closed", "own_url": True, "require_2fa": True},
            hostnames={"layout": "two-domains", "example": {"name": "username", **c.hostnames_for("username")}},
            limits=c.limits,
            extras=[{"kind": "accelerator", "id": "cloudflared", "doc": f"{c.base_url}/docs/cloudflared",
                     "api": f"{c.api}accelerators/cloudflared"}])
        doc = docs.sign_document("hub", payload, [self.key])
        return JSONResponse(doc, headers={"Cache-Control": "max-age=300"})

    # --- invites --------------------------------------------------------------------------

    def invite_state(self, s: dict, code: str) -> dict:
        inv = s["invites"].get(code)
        if inv is None:
            raise HubError(404, "no_such_invite", "no such invite")
        if inv["used_by"]:
            raise HubError(410, "invite_used", "this invite was already used")
        if inv["expires"] < now():
            raise HubError(410, "invite_expired", "this invite has expired")
        return inv

    async def get_invite(self, request: Request):
        with self.store.read() as s:
            inv = self.invite_state(s, request.path_params["code"])
            return JSONResponse(payloads.build_invite_response(format_time(inv["expires"]), self.cfg.name, inv["name_hint"], bool(inv.get("bind"))))

    # --- joining --------------------------------------------------------------------------

    def _admission_checks(self, s: dict, p: dict) -> dict:
        """Invite and name rules, run before and again (atomically) at commit."""
        code, name = p["invite"], p["name"]
        inv = s["invites"].get(code)
        if inv is None:
            raise HubError(403, "needs_invite", "a valid invite is required")
        self.invite_state(s, code)
        if p.get("reregister") is True:
            m = s["members"].get(name)
            if inv.get("reregister") != name or m is None or m["status"] != "active":
                raise HubError(403, "needs_invite", "this invite does not approve re-registering that name")
        else:
            bound = inv.get("bind")
            if bound and bound != name:
                raise HubError(409, "name_bound", "this invite is bound to another name")
            try:
                check_name(name, taken=list(s["members"]), reserved_extra=self.cfg.reserved_extra(), bound=bool(bound))
            except NameRuleError as e:
                raise HubError(409, e.code, e.detail) from e
        return inv

    async def _check_own_urls(self, p: dict, sign_key: Key) -> None:
        urls = p["own_urls"]
        if origin(urls["instance"]) == origin(urls["files"]):
            raise HubError(422, "own_url_unreachable", "the instance and files URLs must be different origins")
        try:
            async with self._http_factory() as c:
                r = await c.get(urls["instance"].rstrip("/") + "/.well-known/l0wagent/self")
                if r.status_code != 200:
                    raise HubError(422, "own_url_unreachable", f"self document answered {r.status_code}")
                sp = payloads.verify_self(r.json())
                if sp["address"] != self.address(p["name"]) or payloads.self_signing_key(sp).kid != sign_key.kid:
                    raise HubError(422, "own_url_unreachable", "the served self document is not this member's")
                await c.get(urls["files"])  # any answer over HTTPS counts
        except HubError:
            raise
        except Exception as e:
            raise HubError(422, "own_url_unreachable", f"{type(e).__name__}: {e}") from e

    async def register(self, request: Request):
        doc = await self.body(request, "bad_registration")
        p = self.parse_signed(payloads.verify_registration, "bad_registration", doc, self.cfg.domain)
        keys = {k.use: k for k in (Key.from_dict(k) for k in p["keys"])}
        if release_tuple(p["instance"]["release"]) < release_tuple(self.cfg.min_release):
            raise HubError(426, "instance_too_old", f"this hub needs instance release {self.cfg.min_release} or newer")
        with self.store.read() as s:
            self._admission_checks(s, p)
        name = p["name"]
        if p["mode"] == "own-url":
            await self._check_own_urls(p, keys["sign"])
            hostnames = {k: urlsplit(p["own_urls"][k]).netloc.lower() for k in ("instance", "files")}
        else:
            hostnames = self.cfg.hostnames_for(name)
        reregistered = p.get("reregister") is True
        with self.store.write() as s:
            self._admission_checks(s, p)
            s["invites"][p["invite"]]["used_by"] = name
            old = s["members"].get(name)
            member = {
                "name": name, "address": self.address(name), "status": "active", "mode": p["mode"],
                "keys": {u: k.to_dict() for u, k in keys.items()}, "kid_history": [k.kid for k in keys.values()],
                "seq": -1, "self_doc": None, "hostnames": hostnames, "own_urls": p["own_urls"],
                "joined": format_time(now()), "release": p["instance"]["release"],
            }
            if reregistered and old:
                old_kids = [k["kid"] for k in old["keys"].values()]
                member["kid_history"] = old["kid_history"] + [k.kid for k in keys.values()]
                s["reregistrations"].setdefault(name, []).append(docs.sign_document(
                    "reregistration", payloads.build_reregistration(
                        member["address"], old_kids, [k.kid for k in keys.values()]), [self.key]))
                for kid in old_kids:
                    s["revocations"].setdefault(name, []).append(docs.sign_document(
                        "revocation", payloads.build_revocation(member["address"], kid, "re-registered"), [self.key]))
            s["members"][name] = member
        if reregistered:
            self.cloudflared.pop(name, None)
            await self.tunnels.drop(name, 4401)
        tunnel = self.cfg.tunnel_url if p["mode"] == "tunnel" else None
        return JSONResponse(payloads.build_join_response(self.address(name), hostnames, self.key.key, tunnel), status_code=201)

    # --- self description -----------------------------------------------------------------

    def _walk_history(self, m: dict, payload: dict) -> dict[str, Key]:
        """Apply the rotations in `history` to the hub's record; return the keys they lead to."""
        cur = {u: Key.from_dict(m["keys"][u]) for u in USES}
        seen = set(m["kid_history"])
        for rdoc in payload["history"]:
            rp = docs.parse_payload(rdoc)
            if rp.get("type") != "rotation" or rp.get("address") != m["address"]:
                raise HubError(400, "bad_self", "history holds a rotation for another address")
            new = Key.from_dict(rp["new"])
            use = next((u for u, k in cur.items() if k.kid == rp.get("old")), None)
            if use is None:
                if new.kid in seen:
                    continue  # applied by an earlier self
                raise HubError(400, "bad_self", "rotation chain does not start at a key the hub knows")
            if new.use != use:
                raise HubError(400, "bad_self", "rotation changes the use of a key")
            self.parse_signed(payloads.verify_rotation, "bad_self", rdoc, cur[use], None if use == "recover" else cur["recover"])
            cur[use] = new
            seen.add(new.kid)
        return cur

    async def put_self(self, request: Request):
        name = request.path_params["name"]
        with self.store.read() as s:
            self._member_or_raise(s, name)
        doc = await self.body(request, "bad_self")
        p = self.parse_signed(payloads.verify_self, "bad_self", doc)
        with self.store.write() as s:
            m = self._member_or_raise(s, name)
            if p["address"] != m["address"]:
                raise HubError(400, "bad_self", "address does not match the member")
            if p["hostnames"] != m["hostnames"]:
                raise HubError(400, "bad_self", "hostnames do not match what the hub assigned")
            if m["mode"] == "own-url" and p.get("own_urls") != m["own_urls"]:
                raise HubError(400, "bad_self", "own_urls do not match the registration")
            expected = self._walk_history(m, p)
            given = {k["use"]: Key.from_dict(k) for k in p["keys"]}
            if any(given[u].kid != expected[u].kid for u in USES):
                raise HubError(400, "bad_self", "keys differ from the hub's record and no valid rotation explains it")
            if p["seq"] <= m["seq"]:
                raise HubError(409, "stale_seq", f"seq must be higher than {m['seq']}")
            m["seq"], m["self_doc"] = p["seq"], doc
            m["keys"] = {u: k.to_dict() for u, k in given.items()}
            m["kid_history"] = sorted(set(m["kid_history"]) | {k.kid for k in given.values()})
        return JSONResponse(docs.countersign_directory(doc, self.key))

    # --- directory ------------------------------------------------------------------------

    async def get_entry(self, request: Request):
        name = request.path_params["name"]
        with self.store.read() as s:
            m = s["members"].get(name)
            if m is None or (m["status"] == "active" and m["self_doc"] is None):
                raise HubError(404, "no_such_member", "no such member (or it has not published yet)")
            if m["status"] == "left":
                if m.get("moved_doc"):
                    return JSONResponse(m["moved_doc"], status_code=301, headers={"Cache-Control": "max-age=3600"})
                raise HubError(410, "left", "this member has left")
            return JSONResponse(docs.countersign_directory(m["self_doc"], self.key), headers={"Cache-Control": "no-cache"})

    async def get_list(self, request: Request):
        name, kind = request.path_params["name"], request.url.path.rsplit("/", 1)[1]
        with self.store.read() as s:
            if name not in s["members"]:
                raise HubError(404, "no_such_member", "no such member")
            return JSONResponse(s[kind].get(name, []))

    # --- leaving --------------------------------------------------------------------------

    async def leave(self, request: Request):
        name = request.path_params["name"]
        with self.store.read() as s:
            m = self._member_or_raise(s, name)
            sign_key = Key.from_dict(m["keys"]["sign"])
        body = await self.body(request, "bad_leave")
        moved_doc = body.pop("moved", None)
        p = self.parse_signed(payloads.verify_leave, "bad_leave", body, sign_key)
        self.parse_signed(check_fresh, "bad_leave", p["iat"])
        if p["address"] != self.address(name):
            raise HubError(400, "bad_leave", "address does not match the member")
        if moved_doc is not None and p["moved_to"] is not None:
            mp = self.parse_signed(payloads.verify_moved, "bad_leave", moved_doc, sign_key)
            if mp["from"] != p["address"] or mp["to"] != p["moved_to"]:
                raise HubError(400, "bad_leave", "moved document does not match the leave document")
        else:
            moved_doc = None
        with self.store.write() as s:
            m = self._member_or_raise(s, name)
            m["status"], m["left_at"], m["moved_to"], m["moved_doc"] = "left", format_time(now()), p["moved_to"], moved_doc
        self.cloudflared.pop(name, None)  # leaving disables the accelerator first
        await self.tunnels.drop(name, 1000)
        return JSONResponse({"v": VERSION, "left": True})

    # --- accelerators ---------------------------------------------------------------------

    async def cloudflared_request(self, request: Request):
        """POST /v0/accelerators/cloudflared (accelerators.md). In-memory state; `activate` never checks
        the tunnel's connection (there is no Cloudflare here)."""
        doc = await self.body(request, "bad_request")
        try:  # the address is read before the signature is checked, only to find the key
            addr = json.loads(b64.decode(doc["payload"]))["address"]
            name, domain = payloads.split_address(addr)
        except Exception as e:
            raise HubError(400, "bad_request", "malformed accelerator-request") from e
        with self.store.read() as s:
            m = s["members"].get(name)
            m = json.loads(json.dumps(m)) if m else None
        if m is None or domain != self.cfg.domain.lower():
            raise HubError(401, "bad_sig", "unknown address")
        try:
            p = payloads.verify_accelerator_request(doc, Key.from_dict(m["keys"]["sign"]), hub=self.cfg.domain,
                                                    address=self.address(name))
        except UnsupportedVersion as e:
            raise HubError(400, "unsupported_version", str(e), {"supported": list(SUPPORTED_VERSIONS)}) from e
        except SignatureError as e:
            raise HubError(401, "bad_sig", str(e)) from e
        except DocumentError as e:
            raise HubError(400, "bad_request", str(e)) from e
        if m["status"] != "active" or m["mode"] != "tunnel":
            raise HubError(403, "not_member", "only active tunnel-mode members may use the accelerator")
        iat = parse_time(p["iat"])
        last = self.cloudflared_iat.get(name)
        if last is not None and iat <= last:
            raise HubError(409, "stale", "iat is not later than the last accepted request", {"last_iat": format_time(last)})
        action, st = p["action"], self.cloudflared.get(name)
        if action == "enable":
            st = self.cloudflared.setdefault(name, {"tunnel_id": str(uuid.uuid4()), "token": "fake-cf-token-" + secrets.token_hex(16),
                                                    "active": False})
            st["service"] = p["service"]
        elif st is None and action != "disable":
            raise HubError(409, "not_enabled", "enable the accelerator first")
        elif action == "activate":
            st["active"] = True
        elif action == "deactivate":
            st["active"] = False
        else:  # disable
            self.cloudflared.pop(name, None)
            st = None
        self.cloudflared_iat[name] = iat
        return JSONResponse({"state": self._cf_state(m, st, token=action == "enable")})

    def _cf_state(self, m: dict, st: dict | None, token: bool = False) -> dict:
        hn = m["hostnames"]
        out = {"enabled": st is not None, "active": bool(st and st["active"]), "tunnel_id": st["tunnel_id"] if st else None,
               "hostnames": [hn["instance"], hn["files"]]}
        if token and st:
            out["token"] = st["token"]
        return out


def create_app(cfg: Config, *, http_client_factory=None):
    """The ASGI app: the hub API, behind the front that sends member hostnames down their tunnels."""
    hub = Hub(cfg, http_client_factory=http_client_factory)

    def wrap(fn):
        async def handler(request: Request):
            try:
                return await fn(request)
            except HubError as e:
                return hub.error(e)
        return handler

    name_re = r"{name:str}"
    routes = [
        Route("/.well-known/l0wagent/hub", wrap(hub.capabilities)),
        Route(f"/.well-known/l0wagent/members/{name_re}", wrap(hub.get_entry)),
        Route(f"/.well-known/l0wagent/members/{name_re}/revocations", wrap(hub.get_list)),
        Route(f"/.well-known/l0wagent/members/{name_re}/reregistrations", wrap(hub.get_list)),
        Route("/v0/invites/{code}", wrap(hub.get_invite)),
        Route("/v0/members", wrap(hub.register), methods=["POST"]),
        Route(f"/v0/members/{name_re}/self", wrap(hub.put_self), methods=["PUT"]),
        Route(f"/v0/members/{name_re}/leave", wrap(hub.leave), methods=["POST"]),
        Route("/v0/accelerators/cloudflared", wrap(hub.cloudflared_request), methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    front = hub.tunnels.asgi(app)
    front.hub = hub  # for tests and the CLI
    return front

