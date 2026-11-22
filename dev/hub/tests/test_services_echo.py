"""The built-in echo service, end to end: a fake member with its own inbox (behind a real tunnel client) says hello,
calls, and says goodbye. Everything it sends is sealed to the member and verified with l0wproto."""

import asyncio
import json
import sys

import httpx
import pytest
from l0wproto import b64, docs, messages, payloads
from l0wproto.keys import Key, generate
from l0wproto.times import now
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from conformance.harness import Inst, Net, new_name
from fakehub.client import TunnelClient

from .conftest import Running

EXT = {"git": "https://git.example/hub-services.git", "commit": "c" * 40, "path": "extensions/echo"}


class Member:
    """A member instance: registered, tunneled, with an inbox that keeps what it receives (opened and verified)."""

    def __init__(self, running: Running, name: str):
        self.r, self.inst, self.inbox, self.arrived = running, Inst("hub.localhost", name), [], asyncio.Event()
        self.echo_sign: Key | None = None
        self.my_tokens: dict[str, dict] = {}
        self.echo_token: dict | None = None
        self.seq = 0

    async def start(self, net: Net):
        r, inst = self.r, self.inst
        code = r.app.hub.store.make_invite()
        resp = await net.http.post(f"{r.url}/v0/members", json=inst.registration(code))
        assert resp.status_code == 201
        body = resp.json()
        inst.hostnames = body["hostnames"]
        assert (await net.http.put(f"{r.url}/v0/members/{inst.name}/self", json=inst.self_doc(0))).status_code == 200
        target = Starlette(routes=[Route("/api/peer/v0/inbox", self.receive, methods=["POST"])])
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=target))
        self.client = TunnelClient(body["tunnel"], inst.address, inst.sign, "hub.localhost",
                                   {inst.hostnames["instance"]: "http://target"}, connect_to=("127.0.0.1", r.port), http=http)
        self.task = asyncio.create_task(self.client.run_forever())
        await asyncio.wait_for(self.client.ready.wait(), 10)
        self.echo_addr = f"echo@{r.cfg.domain}"
        ent = (await net.http.get(f"{r.url}/.well-known/l0wagent/members/echo")).json()
        self.hub_key = r.app.hub.key.key
        p = docs.verify_directory_entry(ent, self.hub_key, hub_domain=r.cfg.domain, name="echo")
        ks = {k["use"]: Key.from_dict(k) for k in p["keys"]}
        self.echo_sign, self.echo_seal = ks["sign"], ks["seal"]

    async def receive(self, request: Request):
        doc = await request.json()
        p = messages.envelope_payload(doc)
        # the member checks the token it issued, and opens the body
        assert p["from"] == self.echo_addr
        tid = messages.verify_delivery_token(p["token"], self.inst.sign.key)["id"]
        assert tid in self.my_tokens
        _p, body = messages.open_envelope(doc, my_address=self.inst.address, my_seal_keys={self.inst.seal.kid: self.inst.seal.private},
                                          sender_sign_key=self.echo_sign)
        self.inbox.append((p["kind"], body))
        self.arrived.set()
        return JSONResponse({"v": "l0w/0.1", "id": p["id"]}, status_code=202)

    async def next(self, kind: str):
        for _ in range(100):
            for i, (k, b) in enumerate(self.inbox):
                if k == kind:
                    del self.inbox[i]
                    return b
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), 0.2)
            except TimeoutError:
                pass
        raise AssertionError(f"no {kind} arrived; got {[k for k, _ in self.inbox]}")

    def token_for_echo(self) -> dict:
        return docs.sign_document("delivery-token", payloads.build_delivery_token(
            self.inst.address, self.inst.sign.kid, self.echo_addr, self.echo_sign.kid), [self.inst.sign])

    def envelope(self, kind: str, body: dict, token="default", *, signer=None, seq=None) -> dict:
        """The member's envelope to echo. For hello `token` is absent; otherwise echo's token for us."""
        self.seq += 1
        return messages.make_envelope(signer or self.inst.sign, (signer or self.inst.sign).kid, self.inst.address, self.echo_addr,
                                      self.echo_seal, kind, body, seq or self.seq, None if token is None else self.echo_token if token == "default" else token)

    async def post(self, net: Net, env: dict, host: str | None = None) -> httpx.Response:
        h = host or self.r.app.hub.echo.hostnames["instance"]
        return await net.http.post(net.url_for_host(h, "/api/peer/v0/inbox"), json=env)

    async def hello(self, net: Net) -> dict:
        tok = self.token_for_echo()
        self.my_tokens[payloads.validate_token_document(tok)["id"]] = tok
        r = await self.post(net, self.envelope("service-hello", {"token": tok, "release": "0.9.0"}, token=None))
        assert r.status_code == 202, r.text
        welcome = await self.next("service-welcome")
        self.echo_token = welcome["token"]
        return welcome

    async def call(self, net: Net, method: str, args: dict | None = None, role=None) -> dict:
        cid = b64.encode(__import__("os").urandom(16))
        r = await self.post(net, self.envelope("service-call", {"call": cid, "method": method, "args": args or {}, "role": role}))
        assert r.status_code == 202, r.text
        reply = await self.next("service-reply")
        assert reply["call"] == cid
        return reply

    async def stop(self):
        self.task.cancel()
        await self.client.aclose()


def run_echo(tmp_path, check, *, tester=False, **cfg):
    """Start a hub with echo; register a member (listed as a tester when asked); run `check(running, net, member)`."""
    name = new_name("mem")

    async def go():
        r = Running(tmp_path, echo=True, echo_testers=[name] if tester else [], **cfg).start()
        net = Net(r.url, r.connect)
        m = Member(r, name)
        try:
            await m.start(net)
            await check(r, net, m)
        finally:
            await m.stop()
            await net.close()
            r.stop()
    asyncio.run(go())


def test_capabilities_list_echo(tmp_path):
    async def check(r, net, m):
        caps = payloads.verify_hub((await net.http.get(f"{r.url}/.well-known/l0wagent/hub")).json())
        found, skipped = payloads.services_in(caps)
        assert skipped == [] and len(found) == 1
        s = found[0]
        assert s["address"] == "echo@hub.localhost" and s["kind"] == "custom" and s["roles"] == ["tester"]
        assert s["default"] == "on" and s["extension"] == EXT
        # its directory entry is the hub's countersigned self document, served without a tunnel
        assert m.echo_sign.kid == r.app.hub.echo.sign.kid

    run_echo(tmp_path, check, echo_extension=EXT)


def test_conformance_service_checks_run_against_echo(tmp_path, capsys):
    from conformance.__main__ import run

    async def suite():
        r = Running(tmp_path, echo=True).start()
        try:
            cmd = (f"{sys.executable} -m fakehub --base-url {r.url} --state {r.cfg.state_path} "
                   f"--reserved {','.join(r.cfg.reserved)} invite")
            return await run(r.url, cmd, connect=r.connect)
        finally:
            r.stop()
    code = asyncio.run(suite())
    out = capsys.readouterr().out
    assert code == 0 and "FAIL" not in out, out
    assert "PASS  service extras[1]: echo@hub.localhost has a directory entry countersigned by the hub" in out, out


def test_hello_welcome_calls_and_bye(tmp_path):
    async def check(r, net, m):
        welcome = await m.hello(net)
        payloads.validate_body("service-welcome", welcome)
        grants = [payloads.verify_role_grant(g, m.echo_sign, service=m.echo_addr, holder=m.inst.address, holder_kid=m.inst.sign.kid)
                  for g in welcome["roles"]]
        assert [g["role"] for g in grants] == ["tester"]
        grant = welcome["roles"][0]
        assert (await m.call(net, "echo", {"a": [1, 2], "b": "é"}))["result"] == {"a": [1, 2], "b": "é"}
        assert (await m.call(net, "whoami"))["result"] == {"address": m.inst.address, "roles": []}
        assert (await m.call(net, "whoami", role=grant))["result"] == {"address": m.inst.address, "roles": ["tester"]}
        no = await m.call(net, "secret")
        assert no["ok"] is False and no["error"]["code"] == "not_allowed"
        assert (await m.call(net, "secret", role=grant))["result"] == {"secret": "xyzzy"}
        assert (await m.call(net, "nope"))["error"]["code"] == "unknown_method"
        # a grant signed by anyone but the service is not honored
        fake = docs.sign_document("role-grant", payloads.build_role_grant(m.echo_addr, m.inst.address, m.inst.sign.kid, "tester"),
                                  [generate("sign")])
        assert (await m.call(net, "secret", role=fake))["error"]["code"] == "not_allowed"
        # bye: the member is forgotten and its token with it
        assert (await m.post(net, m.envelope("service-bye", {}))).status_code == 202
        r2 = await m.post(net, m.envelope("service-call", {"call": b64.encode(bytes(16)), "method": "echo", "args": {}}))
        assert r2.status_code == 403 and r2.json()["error"] == "no_token"

    run_echo(tmp_path, check, tester=True)


def test_no_grant_for_members_not_listed(tmp_path):
    async def check(r, net, m):
        assert (await m.hello(net))["roles"] == []
        assert (await m.call(net, "secret"))["error"]["code"] == "not_allowed"
        # even a grant the service signed is not honored for a member the config does not list (it stopped honoring it)
        g = docs.sign_document("role-grant", payloads.build_role_grant(m.echo_addr, m.inst.address, m.inst.sign.kid, "tester"),
                               [r.app.hub.echo.sign])
        assert (await m.call(net, "secret", role=g))["error"]["code"] == "not_allowed"

    run_echo(tmp_path, check)


def test_refusals(tmp_path):
    async def check(r, net, m):
        call = {"call": b64.encode(bytes(16)), "method": "echo", "args": {}}
        # hello from a non-member of this hub
        outsider = Inst("hub.localhost", "nobody")
        tok = docs.sign_document("delivery-token", payloads.build_delivery_token(
            outsider.address, outsider.sign.kid, m.echo_addr, m.echo_sign.kid), [outsider.sign])
        env = messages.make_envelope(outsider.sign, outsider.sign.kid, outsider.address, m.echo_addr, m.echo_seal, "service-hello",
                                     {"token": tok, "release": "0.9.0"}, 1, None)
        resp = await m.post(net, env)
        assert resp.status_code == 403 and resp.json()["error"] == "not_member"
        # a hello whose token is not the member's own
        other_tok = docs.sign_document("delivery-token", payloads.build_delivery_token(
            m.inst.address, m.inst.sign.kid, m.echo_addr, m.echo_sign.kid), [generate("sign")])
        resp = await m.post(net, m.envelope("service-hello", {"token": other_tok, "release": "0.9.0"}, token=None))
        assert resp.status_code == 400 and resp.json()["error"] == "bad_body"
        # a call before hello has no token to show
        resp = await m.post(net, m.envelope("service-call", call, token=m.token_for_echo()))
        assert resp.status_code == 403 and resp.json()["error"] == "no_token"
        await m.hello(net)
        # a forged signature, a wrong recipient, a duplicate
        resp = await m.post(net, m.envelope("service-call", call, signer=generate("sign")))
        assert resp.status_code == 401 and resp.json()["error"] == "bad_signature"
        env = m.envelope("service-call", call)
        assert (await m.post(net, env)).status_code == 202
        again = await m.post(net, env)
        assert again.status_code == 200 and again.json() == {"duplicate": True}
        assert (await m.next("service-reply"))["ok"] is True
        wrong = messages.make_envelope(m.inst.sign, m.inst.sign.kid, m.inst.address, "other@hub.localhost", m.echo_seal, "service-call",
                                       call, 99, m.echo_token)
        resp = await m.post(net, wrong)
        assert resp.status_code == 404 and resp.json()["error"] == "not_here"
        assert (await m.post(net, {"nope": 1})).status_code == 400

    run_echo(tmp_path, check)


def test_echo_is_off_by_default(running):
    async def go():
        net = Net(running.url, running.connect)
        try:
            caps = payloads.verify_hub((await net.http.get(f"{running.url}/.well-known/l0wagent/hub")).json())
            assert payloads.services_in(caps) == ([], [])
            assert (await net.http.get(f"{running.url}/.well-known/l0wagent/members/echo")).status_code == 404
        finally:
            await net.close()
    asyncio.run(go())
