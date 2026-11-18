"""The cloudflared accelerator: lifecycle against a fake cloudflared and a fake hub endpoint, and the local listener."""

from __future__ import annotations

import asyncio
import logging
import stat
import sys
import textwrap

import httpx
import pytest
from l0wproto import payloads
from l0wproto.keys import Key
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .conftest import Threaded, free_port, until

TOKEN = "SECRET-TUNNEL-TOKEN-xyz"

FAKE_CLOUDFLARED = textwrap.dedent('''\
    #!{python}
    import http.server, os, sys
    d = {dir!r}
    argv = sys.argv[1:]
    open(d + "/argv", "a").write(" ".join(argv) + "\\n")
    open(d + "/env-token", "w").write(os.environ.get("TUNNEL_TOKEN", ""))
    open(d + "/env-keys", "w").write(",".join(sorted(os.environ)))
    host, _, port = argv[argv.index("--metrics") + 1].rpartition(":")
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            up = open(d + "/up").read().strip() == "up" if os.path.exists(d + "/up") else False
            self.send_response(200 if up else 503); self.send_header("content-length", "0"); self.end_headers()
        def log_message(self, *a): pass
    http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
''')


class FakeHubAccel:
    """The hub's accelerator endpoint, as far as the instance can tell."""

    def __init__(self, key: Key, hub: str, address: str):
        self.key, self.hub, self.address = key, hub, address
        self.actions: list[str] = []
        self.services: list[str | None] = []
        self.failures: list[tuple[int, str]] = []  # popped, one per request
        self.last_iat = ""
        self.app = Starlette(routes=[Route("/accel", self._post, methods=["POST"])])
        self.srv = Threaded(self.app).start()

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.srv.port}/accel"

    async def _post(self, request: Request):
        p = payloads.verify_accelerator_request(await request.json(), self.key, hub=self.hub, address=self.address)
        if self.failures:
            status, code = self.failures.pop(0)
            return JSONResponse({"error": code}, status)
        if p["iat"] <= self.last_iat:
            return JSONResponse({"error": "stale"}, 409)
        self.last_iat = p["iat"]
        self.actions.append(p["action"])
        self.services.append(p.get("service"))
        st = {"enabled": p["action"] != "disable", "active": p["action"] in ("activate",), "tunnel_id": "t1", "hostnames": []}
        if p["action"] == "enable":
            st["token"] = TOKEN
        return JSONResponse({"state": st})


class Fixture:
    def __init__(self, rig, tmp_path):
        self.rig, self.dir = rig, tmp_path / "cf"
        self.dir.mkdir()
        self.bin = self.dir / "cloudflared"
        self.bin.write_text(FAKE_CLOUDFLARED.format(python=sys.executable, dir=str(self.dir)))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.hub = FakeHubAccel(Key.from_dict(rig.inst.sign.key.to_dict()), "hub.localhost", rig.inst.address)
        self.metrics = f"127.0.0.1:{free_port()}"
        self.port = free_port()

    def up(self, up: bool = True) -> None:
        (self.dir / "up").write_text("up" if up else "down")

    def enable(self, on: bool = True) -> None:
        self.rig.gw.config = {**self.rig.config(), "accelerator": {"enabled": on, "api": self.hub.api}}

    def tune(self, svc, **kw) -> None:
        a = svc.accel
        a.tick, a.ready_after, a.down_after, a.stale_delay = 0.1, 0.5, 0.8, 0.1
        a.min_delay, a.max_delay = 0.1, 0.4
        a.port, a.metrics, a.cloudflared = self.port, self.metrics, str(self.bin)
        for k, v in kw.items():
            setattr(a, k, v)


@pytest.fixture
def fx(rig, tmp_path):
    f = Fixture(rig, tmp_path)
    yield f
    f.hub.srv.stop()


def test_enable_start_ready_activate(rig, run_service, fx, caplog):
    caplog.set_level(logging.DEBUG)
    fx.up()

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions == ["enable", "activate"])
        await until(lambda: "active" in rig.gw.accel_states)
        assert fx.hub.services[0] == f"http://127.0.0.1:{fx.port}" and fx.hub.services[1] is None
        assert rig.gw.accel_states[0] == "starting" or rig.gw.accel_states[0] == "off"
        # the token reached cloudflared by environment only, with nothing else of ours
        assert (fx.dir / "env-token").read_text() == TOKEN
        assert TOKEN not in (fx.dir / "argv").read_text()
        assert "TUNNEL_GATEWAY_TOKEN" not in (fx.dir / "env-keys").read_text()
        assert "--no-autoupdate" in (fx.dir / "argv").read_text()
        # the spec tunnel is still connected throughout
        assert rig.gw.statuses[-1]["state"] == "connected" and rig.gw.statuses[-1]["accelerator"]["state"] == "active"
    run_service(body)
    assert TOKEN not in caplog.text


def test_not_ready_deactivates_and_reactivates(rig, run_service, fx):
    fx.up()

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        fx.up(False)
        await until(lambda: fx.hub.actions[-1:] == ["deactivate"])
        await until(lambda: rig.gw.accel_states[-1] == "starting")
        fx.up(True)
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        assert fx.hub.actions == ["enable", "activate", "deactivate", "activate"]
    run_service(body)


def test_never_ready_never_activates(rig, run_service, fx):
    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions == ["enable"])
        await asyncio.sleep(1.5)
        assert fx.hub.actions == ["enable"] and rig.gw.accel_states[-1] == "starting"
    run_service(body)


def test_disable_on_config_change_stops_cloudflared(rig, run_service, fx):
    fx.up()

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        spec_sessions = [s["session"] for s in rig.gw.statuses if s["state"] == "connected"]
        fx.enable(False)
        await until(lambda: fx.hub.actions[-1:] == ["disable"])
        await until(lambda: rig.gw.accel_states[-1] == "off")
        assert svc.accel._proc is None
        async with httpx.AsyncClient() as c:
            with pytest.raises(httpx.HTTPError):
                await c.get(f"http://{fx.metrics}/ready", timeout=1)
        # the spec tunnel was not touched by any of it
        assert [s["state"] for s in rig.gw.statuses].count("connecting") == 1
        assert {s["session"] for s in rig.gw.statuses if s["state"] == "connected"} == set(spec_sessions)
    run_service(body)


def test_shutdown_stops_cloudflared_without_telling_the_hub(rig, run_service, fx):
    fx.up()
    seen = {}

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        seen["proc"] = svc.accel._proc
    run_service(body)
    assert seen["proc"].returncode is not None
    assert fx.hub.actions == ["enable", "activate"]


def test_stale_is_retried_once_and_not_connected_later(rig, run_service, fx):
    fx.up()
    fx.hub.failures = [(409, "stale")]

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[:1] == ["enable"])
        fx.hub.failures = [(409, "not_connected")]
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        assert len(rig.gw.accel_requests) >= 4  # enable twice (stale), activate twice (not_connected)
    run_service(body)


def test_5xx_backs_off_and_reports_error(rig, run_service, fx):
    fx.hub.failures = [(502, "upstream"), (502, "upstream")]

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: "error" in rig.gw.accel_states)
        await until(lambda: fx.hub.actions == ["enable"])
        err = next(s["accelerator"] for s in rig.gw.statuses if s.get("accelerator", {}).get("state") == "error")
        assert err["error"].startswith("enable:") and TOKEN not in err["error"]
    run_service(body)


def test_cloudflared_restarts_when_it_exits(rig, run_service, fx):
    fx.up()

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[-1:] == ["activate"])
        first = svc.accel._proc
        first.kill()
        await until(lambda: svc.accel._proc is not None and svc.accel._proc is not first and svc.accel._proc.returncode is None)
    run_service(body)


# --- the local listener ---


def test_listener_routes_by_host_and_sets_the_client_header(rig, run_service, fx):
    fx.up()

    async def body(svc):
        fx.tune(svc)
        fx.enable()
        await until(lambda: fx.hub.actions[:1] == ["enable"])
        await until(lambda: svc.accel._server is not None and svc.accel._server.started)
        base = f"http://127.0.0.1:{fx.port}"
        async with httpx.AsyncClient(base_url=base, timeout=10) as c:
            h = rig.inst.hostnames
            r = await c.get("/api/x?y=1", headers={"host": h["instance"], "cf-connecting-ip": "203.0.113.9", "x-l0w-tunnel-client": "6.6.6.6", "x-L0W-Tunnel-Extra": "1",
                                                    "x-custom": "ok"})
            j = r.json()
            assert r.status_code == 200 and j["target"] == "control" and j["path"] == "/api/x" and j["query"] == "y=1"
            hd = j["headers"]
            assert dict(hd)["x-l0w-tunnel-client"] == "203.0.113.9" and sum(1 for k, _ in hd if k == "x-l0w-tunnel-client") == 1
            assert not any(k.startswith("x-l0w-tunnel-") and k != "x-l0w-tunnel-client" for k, _ in hd)
            assert dict(hd)["host"] == h["instance"] and dict(hd)["x-custom"] == "ok"
            r = await c.post("/up", content=b"x" * 500_000, headers={"host": h["files"], "cf-connecting-ip": "203.0.113.9"})
            assert r.json()["target"] == "files" and r.json()["len"] == 500_000
            # a spoofed client header is never used: without CF-Connecting-IP the request is refused
            r = await c.get("/", headers={"host": h["instance"], "x-l0w-tunnel-client": "6.6.6.6"})
            assert r.status_code == 400
            r = await c.get("/", headers={"host": "evil.example", "cf-connecting-ip": "1.1.1.1"})
            assert r.status_code == 421
            r = await c.get("/", headers={"host": h["instance"], "cf-connecting-ip": "1.1.1.1", "connection": "upgrade", "upgrade": "websocket"})
            assert r.status_code == 501
            # streamed responses arrive incrementally
            async with c.stream("GET", "/stream", headers={"host": h["instance"], "cf-connecting-ip": "1.1.1.1"}) as r:
                chunks = [ch async for ch in r.aiter_bytes()]
            assert b"".join(chunks) == b"chunk0\nchunk1\nchunk2\nchunk3\n" and len(chunks) >= 3
            r = await c.get("/boom", headers={"host": h["files"], "cf-connecting-ip": "1.1.1.1"})
            assert "keep-alive" not in r.headers
    run_service(body)
