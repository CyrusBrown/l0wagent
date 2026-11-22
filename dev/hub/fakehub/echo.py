"""The built-in test service `echo@<hub domain>` (protocol/services.md), for development and for testing instances.

It is a directory member (own keys, self document, the hub's countersignature) whose hostname the fake hub serves
itself, with no tunnel. Its inbox accepts `service-hello` from members of this hub only, and answers
`service-welcome` (its token, and a `tester` role grant for the members the config lists). Calls: `echo` (returns
the args), `whoami` (the caller and the roles it honored), `secret` (needs the `tester` role). Replies and
welcomes are separate envelopes, delivered to the member's instance hostname through the hub's own routing, as any
visitor would. Everything uses l0wproto; nothing here is a model for a real service beyond that.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
from l0wproto import VERSION, b64, docs, keys, messages, payloads
from l0wproto.consts import MAX_ENVELOPE
from l0wproto.errors import ProtocolError, SignatureError
from l0wproto.keys import Key
from l0wproto.times import format_time, now
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

NAME = "echo"
ROLES = ["tester"]
CREATED = "2026-11-17T00:00:00Z"
STATUS = {"not_here": 404, "bad_signature": 401, "bad_seal": 400, "bad_body": 400, "bad_document": 400, "unknown_kind": 422,
          "unsupported_version": 400, "too_large": 413}


class Refuse(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


class EchoService:
    def __init__(self, hub):
        self.hub = hub
        cfg = hub.cfg
        self.address = f"{NAME}@{cfg.domain}"
        seed = bytes.fromhex(cfg.seed_path.read_text().strip())
        mk = lambda use: keys.from_seed(hashlib.sha256(seed + b"fakehub-echo/" + use.encode()).digest(), use, CREATED)  # noqa: E731
        self.sign, self.seal, self.recover = mk("sign"), mk("seal"), mk("recover")
        self.hostnames = cfg.hostnames_for(NAME)
        self.testers = {self._addr(t) for t in cfg.echo_testers}
        self.seen: set[str] = set()
        self._seq = int(now())
        self._tasks: set[asyncio.Task] = set()
        #: what was sent to members: (kind, to, HTTP status or None), newest last; for tests and curiosity
        self.sent: list[tuple[str, str, int | None]] = []
        self.app = Starlette(routes=[Route("/api/peer/v0/inbox", self.inbox, methods=["POST"])])
        self.register()

    def _addr(self, who: str) -> str:
        return who.lower() if "@" in who else f"{who.lower()}@{self.hub.cfg.domain}"

    # --- the directory entry and the capabilities extra -------------------------------------

    def register(self) -> None:
        """Make the directory member (idempotent; the hub's key seed fixes the keys across restarts)."""
        with self.hub.store.write() as s:
            old = s["members"].get(NAME)
            if old and old.get("builtin") and old["hostnames"] == self.hostnames:
                return
            p = payloads.build_self(self.address, 1, [self.sign.key, self.seal.key, self.recover.key], self.hostnames)
            s["members"][NAME] = {
                "name": NAME, "address": self.address, "status": "active", "builtin": True,
                "keys": {k.key.use: k.key.to_dict() for k in (self.sign, self.seal, self.recover)},
                "kid_history": [self.sign.kid, self.seal.kid, self.recover.kid], "seq": 1,
                "self_doc": docs.sign_document("self", p, [self.sign]), "hostnames": self.hostnames,
                "joined": format_time(now()), "release": "0.9.0"}

    def service_extra(self) -> dict:
        cfg = self.hub.cfg
        https = cfg.base_url.startswith("https://")
        p = payloads.build_service(self.address, "custom", ROLES, title="Echo", description="A test service: echo, whoami, secret.",
                                   docs=f"{cfg.base_url}/docs/echo" if https else None, extension=cfg.echo_extension, default="on")
        return {"kind": "service", "doc": docs.sign_document("service", p, [self.hub.key])}

    def owns_host(self, host: str) -> bool:
        return host.rsplit(":", 1)[0] == self.hostnames["instance"].lower().rsplit(":", 1)[0]

    # --- state -------------------------------------------------------------------------------

    def _state(self, s: dict) -> dict:
        return s.setdefault("echo", {"members": {}, "tokens": {}})

    def _member(self, addr: str) -> dict | None:
        name, domain = payloads.split_address(addr)
        if domain != self.hub.cfg.domain.lower() or name == NAME:
            return None
        m = self.hub.store.member(name)
        return m if m and m["status"] == "active" and m.get("self_doc") and not m.get("builtin") else None

    @staticmethod
    def _keys_of(m: dict) -> tuple[Key, Key]:
        p = json.loads(b64.decode(m["self_doc"]["payload"]))
        ks = {k["use"]: Key.from_dict(k) for k in p["keys"]}
        return ks["sign"], ks["seal"]

    # --- receiving ---------------------------------------------------------------------------

    async def inbox(self, request: Request):
        try:
            raw = await request.body()
            if len(raw) > MAX_ENVELOPE:
                raise Refuse(413, "too_large", "envelope is over 16 MiB")
            try:
                doc = json.loads(raw)
                p = messages.envelope_payload(doc)
            except ProtocolError as e:
                raise Refuse(STATUS.get(e.code, 400), e.code, str(e)) from e
            except ValueError as e:
                raise Refuse(400, "bad_document", "body is not JSON") from e
            if p["to"].lower() != self.address:
                raise Refuse(404, "not_here", "not this address")
            sender = self._member(p["from"])
            if sender is None:
                raise Refuse(403, "not_member", "only members of this hub may use this service")
            sign_key, _seal = self._keys_of(sender)
            if p["kind"] == "service-hello":
                pass
            else:
                tok = self._check_token(p)
                if tok is None:
                    raise Refuse(403, "no_token", "no token")
            if p["from_kid"] != sign_key.kid:
                raise Refuse(401, "bad_signature", "")
            try:
                _p, body = messages.open_envelope(doc, my_address=self.address, my_seal_keys={self.seal.kid: self.seal.private},
                                                  sender_sign_key=sign_key)
            except ProtocolError as e:
                code = "bad_signature" if isinstance(e, SignatureError) else e.code
                raise Refuse(STATUS.get(code, 400), code, "" if code == "bad_signature" else str(e)) from e
            if p["id"] in self.seen:
                return JSONResponse({"duplicate": True})
            self.seen.add(p["id"])
            await self.handle(p, body, sender)
            return JSONResponse({"v": VERSION, "id": p["id"]}, status_code=202)
        except Refuse as e:
            return JSONResponse({"v": VERSION, "error": e.code, "detail": e.detail}, status_code=e.status)

    def _check_token(self, p: dict) -> dict | None:
        """The token this service issued, to this sender (or a key that rotated from the one it was issued to)."""
        try:
            tid = messages.verify_delivery_token(p["token"], self.sign.key)["id"] if "token" in p else None
            with self.hub.store.read() as s:
                rec = self._state(s)["tokens"].get(tid)
                m = s["members"].get(payloads.split_address(p["from"])[0])
        except Exception:
            return None
        if not rec or rec["holder"] != p["from"].lower() or not m:
            return None
        if rec["holder_kid"] != p["from_kid"] and rec["holder_kid"] not in m["kid_history"]:
            return None
        return rec

    # --- behavior ----------------------------------------------------------------------------

    async def handle(self, p: dict, body: dict, sender: dict) -> None:
        addr, kid = p["from"].lower(), p["from_kid"]
        if p["kind"] == "service-hello":
            sign_key, _ = self._keys_of(sender)
            try:  # their token for us: theirs, for our signing key
                messages.verify_delivery_token(body["token"], sign_key, self.sign.kid)
            except ProtocolError as e:
                raise Refuse(400, "bad_body", "the token is not theirs for this service") from e
            theirs = payloads.validate_token_document(body["token"])
            if theirs["issuer"].lower() != addr or theirs["holder"].lower() != self.address:
                raise Refuse(400, "bad_body", "the token is not theirs for this service")
            mine = docs.sign_document("delivery-token", payloads.build_delivery_token(self.address, self.sign.kid, addr, kid), [self.sign])
            tid = payloads.validate_token_document(mine)["id"]
            with self.hub.store.write() as s:
                st = self._state(s)
                old = st["members"].get(addr)
                if old:
                    st["tokens"].pop(old["out_id"], None)
                st["members"][addr] = {"their_token": body["token"], "out_id": tid}
                st["tokens"][tid] = {"holder": addr, "holder_kid": kid}
            grants = [self.grant(addr, kid)] if addr in self.testers else []
            self.deliver("service-welcome", addr, {"token": mine, "roles": grants})
        elif p["kind"] == "service-bye":
            with self.hub.store.write() as s:
                st = self._state(s)
                old = st["members"].pop(addr, None)
                if old:
                    st["tokens"].pop(old["out_id"], None)
        elif p["kind"] == "service-call":
            self.deliver("service-reply", addr, self.call(p, body))

    def grant(self, holder: str, holder_kid: str) -> dict:
        return docs.sign_document("role-grant", payloads.build_role_grant(self.address, holder, holder_kid, "tester"), [self.sign])

    def honored_roles(self, p: dict, body: dict) -> list[str]:
        """The roles of the grant a call carries, when it is ours, for this sender, in date, and still configured."""
        g = body.get("role")
        if g is None:
            return []
        try:
            r = payloads.verify_role_grant(g, self.sign.key, service=self.address, holder=p["from"], holder_kid=p["from_kid"])
        except Exception:
            return []
        return [r["role"]] if r["role"] in ROLES and p["from"].lower() in self.testers else []

    def call(self, p: dict, body: dict) -> dict:
        def ok(result):
            return {"call": body["call"], "ok": True, "result": result}

        def no(code, detail):
            return {"call": body["call"], "ok": False, "error": {"code": code, "detail": detail}}

        roles = self.honored_roles(p, body)
        method = body["method"]
        if method == "echo":
            return ok(body["args"])
        if method == "whoami":
            return ok({"address": p["from"].lower(), "roles": roles})
        if method == "secret":
            return ok({"secret": "xyzzy"}) if "tester" in roles else no("not_allowed", "this method needs the tester role")
        return no("unknown_method", f"no method {method!r}")

    # --- sending -----------------------------------------------------------------------------

    def deliver(self, kind: str, to: str, body: dict) -> None:
        """Send after the inbox has answered 202 (the reply is its own envelope)."""
        t = asyncio.get_running_loop().create_task(self._send(kind, to, body))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for deliveries in flight (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _send(self, kind: str, to: str, body: dict) -> None:
        status = None
        try:
            m = self._member(to)
            with self.hub.store.read() as s:
                rec = self._state(s)["members"].get(to)
            if m is None or rec is None:
                return
            _sign, seal_key = self._keys_of(m)
            self._seq += 1
            token = None if kind == "service-hello" else rec["their_token"]
            env = messages.make_envelope(self.sign, self.sign.kid, self.address, to, seal_key, kind, body, self._seq, token)
            cfg = self.hub.cfg
            host = m["hostnames"]["instance"]
            # through the hub's own routing, as any visitor: connect to our listener, send the member's Host
            if cfg.base_url.startswith("http://"):
                url, headers = f"http://{cfg.listen_host}:{cfg.listen_port}/api/peer/v0/inbox", {"Host": host}
            else:
                url, headers = f"https://{host}/api/peer/v0/inbox", {}
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(url, json=env, headers=headers)
            status = r.status_code
        except Exception:
            pass  # the member is offline or gone: a real service would retry
        finally:
            self.sent.append((kind, to, status))
