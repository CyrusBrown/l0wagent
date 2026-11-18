import asyncio
import sys

import httpx
import pytest
from l0wproto import docs, payloads
from l0wproto.keys import Key
from l0wproto.invite import parse_invite

from conformance.harness import Inst, Net, new_name
from fakehub.client import TunnelClient
from fakehub.config import Config
from fakehub.hub import create_app

from .conftest import Running


def invite_cmd(r: Running) -> str:
    return f"{sys.executable} -m fakehub --base-url {r.url} --state {r.cfg.state_path} invite"


def test_whole_conformance_suite(running, capsys):
    from conformance.__main__ import run

    code = asyncio.run(run(running.url, invite_cmd(running), connect=running.connect))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out
    assert out.count("PASS") >= 70
    assert out.count(" cloudflared: ") >= 7, out  # the accelerator checks ran (not skipped)


def test_hostname_layout_and_config():
    c = Config(base_url="http://hub.localhost:8700", state_path="x.json")
    assert c.domain == "hub.localhost"
    h = c.hostnames_for("mara")
    assert h == {"instance": "mara.hub.localhost:8700", "files": "files--mara.hubfiles.localhost:8700"}
    assert c.tunnel_url == "ws://hub.localhost:8700/v0/tunnel"
    assert Config(base_url="https://hub.example", state_path="x.json").tunnel_url == "wss://hub.example/v0/tunnel"


def test_invite_cli_and_lookup(running):
    import subprocess

    out = subprocess.run(invite_cmd(running).split() + ["--name-hint", "mara"], capture_output=True, text=True, check=True).stdout
    inv = parse_invite(out.strip(), allow_insecure=True)
    assert inv.base_url == running.url

    async def go():
        net = Net(running.url, running.connect)
        try:
            r = await net.http.get(f"{running.url}/v0/invites/{inv.code}")
            assert r.status_code == 200 and r.json()["name_hint"] == "mara"
            r = await net.http.get(f"{running.url}/v0/invites/{inv.code}")
            assert r.status_code == 200  # lookup does not consume
        finally:
            await net.close()
    asyncio.run(go())


def test_restart_keeps_members(tmp_path):
    a = Running(tmp_path).start()
    inst = None

    async def join(r):
        nonlocal inst
        net = Net(r.url, r.connect)
        try:
            code = a.app.hub.store.make_invite()
            inst = Inst("hub.localhost", new_name())
            resp = await net.http.post(f"{r.url}/v0/members", json=inst.registration(code))
            assert resp.status_code == 201, resp.text
            inst.hostnames = resp.json()["hostnames"]
            assert (await net.http.put(f"{r.url}/v0/members/{inst.name}/self", json=inst.self_doc(0))).status_code == 200
        finally:
            await net.close()
    asyncio.run(join(a))
    key_before = a.app.hub.key.kid
    a.stop()
    b = Running(tmp_path)
    b.cfg.base_url = a.cfg.base_url  # same hub, same state
    b.app = create_app(a.cfg)
    assert b.app.hub.key.kid == key_before  # fixed hub key from the seed file
    assert b.app.hub.store.member(inst.name)["status"] == "active"
    assert b.app.hub.store.member(inst.name)["seq"] == 0


def test_own_url_registration_checked_by_the_hub(tmp_path):
    """Own-url mode: the hub fetches the self document from the member's own instance URL."""
    inst = Inst("hub.localhost", new_name())
    urls = {"instance": "https://box.example", "files": "https://files.example"}
    inst.hostnames = {"instance": "box.example", "files": "files.example"}
    selfdoc = inst.self_doc(0)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "box.example" and req.url.path == "/.well-known/l0wagent/self":
            return httpx.Response(200, json=selfdoc)
        return httpx.Response(200, text="files")

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    cfg = Config(base_url="http://hub.localhost:8700", state_path=tmp_path / "s.json")
    app = create_app(cfg, http_client_factory=factory)
    hub = app.hub

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub.localhost:8700") as c:
            code = hub.store.make_invite()
            r = await c.post("/v0/members", json=inst.registration(code, mode="own-url", own_urls=urls))
            assert r.status_code == 201, r.text
            assert r.json()["hostnames"] == inst.hostnames and r.json()["tunnel"] is None
            # a second one whose instance URL serves someone else's self document is refused
            other = Inst("hub.localhost", new_name())
            r = await c.post("/v0/members", json=other.registration(hub.store.make_invite(), mode="own-url", own_urls=urls))
            assert r.status_code == 422 and r.json()["error"] == "own_url_unreachable"
            # own-url members are not reached through a tunnel: their hostnames are not the hub's to serve
            r = await c.get("/", headers={"host": "box.example"})
            assert r.status_code == 404
    asyncio.run(go())


def test_reference_client_forwards_to_a_local_target(running):
    """The reference tunnel client: connect, sign the hello, forward by Host, stream a body both ways."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    seen = {}

    async def echo(request: Request):
        body = await request.body()
        seen["client"] = request.headers.get("x-l0w-tunnel-client")
        seen["host"] = request.headers.get("host")
        return JSONResponse({"len": len(body), "path": request.url.path})

    target = Starlette(routes=[Route("/{p:path}", echo, methods=["GET", "POST"])])

    async def go():
        net = Net(running.url, running.connect)
        try:
            inst = Inst("hub.localhost", new_name())
            code = running.app.hub.store.make_invite()
            r = await net.http.post(f"{running.url}/v0/members", json=inst.registration(code))
            assert r.status_code == 201
            body = r.json()
            inst.hostnames = body["hostnames"]
            await net.http.put(f"{running.url}/v0/members/{inst.name}/self", json=inst.self_doc(0))
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=target))
            client = TunnelClient(body["tunnel"], inst.address, inst.sign, "hub.localhost",
                                  {inst.hostnames["instance"]: "http://target", inst.hostnames["files"]: "http://target"},
                                  connect_to=("127.0.0.1", running.port), http=http)
            task = asyncio.create_task(client.run_forever())
            await asyncio.wait_for(client.ready.wait(), 10)
            url = net.url_for_host(inst.hostnames["instance"], "/hello")
            g = await net.http.get(url)
            assert g.status_code == 200 and g.json() == {"len": 0, "path": "/hello"}
            assert seen["client"] == "127.0.0.1" and seen["host"] == inst.hostnames["instance"]
            payload = b"x" * 700_000
            p = await net.http.post(net.url_for_host(inst.hostnames["files"], "/up"), content=payload)
            assert p.json() == {"len": 700_000, "path": "/up"}
            # an unknown host on a live tunnel is the client's business: 421 (only reachable through the client's own map)
            task.cancel()
            await client.aclose()
        finally:
            await net.close()
    asyncio.run(go())


def test_offline_member_and_unknown_hosts(running):
    async def go():
        net = Net(running.url, running.connect)
        try:
            inst = Inst("hub.localhost", new_name())
            r = await net.http.post(f"{running.url}/v0/members", json=inst.registration(running.app.hub.store.make_invite()))
            hn = r.json()["hostnames"]
            r = await net.http.get(net.url_for_host(hn["instance"], "/"), headers={"accept": "application/json"})
            assert r.status_code == 502 and r.headers["retry-after"] == "30" and r.json()["error"] == "instance_offline"
            r = await net.http.get(net.url_for_host(hn["instance"], "/"), headers={"accept": "text/html"})
            assert r.status_code == 502 and r.headers["content-type"].startswith("text/html")
            r = await net.http.get(net.url_for_host("nobody.hub.localhost:1", "/"))
            assert r.status_code == 404
        finally:
            await net.close()
    asyncio.run(go())


def test_hub_key_matches_capabilities(running):
    async def go():
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{running.port}/.well-known/l0wagent/hub")
            p = payloads.verify_hub(r.json())
            k = Key.from_dict(p["keys"][0])
            assert k.kid == running.app.hub.key.kid
            assert p["domain"] == "hub.localhost" and p["api"] == running.url + "/v0/"
            assert p["membership"]["require_2fa"] is True
            assert docs  # imported for the signature check above
    asyncio.run(go())


@pytest.mark.parametrize("accept,json_", [("application/json", True), ("text/html,application/json;q=0.5", False), ("*/*", False),
                                          ("application/json, text/plain", True), ("", False)])
def test_accept_preference(accept, json_):
    from fakehub.tunnel import prefers_json

    assert prefers_json(accept) is json_


def test_suite_fails_a_hub_that_forwards_smuggled_headers(tmp_path, monkeypatch, capsys):
    """The suite has to be able to fail: a hub that does not strip hop-by-hop and x-l0w-tunnel-* headers."""
    from l0wproto import tunnel as lt

    monkeypatch.setattr(lt, "filter_request_headers", lambda hs: [[k.lower(), v] for k, v in hs])
    r = Running(tmp_path).start()
    try:
        from conformance.__main__ import run

        code = asyncio.run(run(r.url, invite_cmd(r), connect=r.connect))
    finally:
        r.stop()
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  tunnel: GET by the instance hostname" in out
