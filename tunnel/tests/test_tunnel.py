import time

import httpx
from l0wproto import keys

from .conftest import Hub, until


def test_hosts_reach_their_targets_and_the_client_header_is_ours(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        assert rig.gw.auth_seen == {"Bearer secret-token"}
        async with rig.visit("instance") as c:
            r = await c.get("/api/x?y=1", headers={"x-l0w-tunnel-client": "6.6.6.6", "connection": "close", "x-custom": "ok"})
            j = r.json()
            assert r.status_code == 200 and j["target"] == "control" and j["path"] == "/api/x" and j["query"] == "y=1"
            h = dict(j["headers"])
            assert h["x-l0w-tunnel-client"] == "127.0.0.1"  # what the hub saw, not what the visitor said
            assert h["host"] == rig.inst.hostnames["instance"] and h["x-custom"] == "ok"
            assert sum(1 for k, _ in j["headers"] if k == "x-l0w-tunnel-client") == 1
        async with rig.visit("files") as c:
            r = await c.get("/f")
            assert r.json()["target"] == "files"
            r = await c.get("/boom")
            assert "keep-alive" not in r.headers
    run_service(body)


def test_unknown_host_is_421(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        # the hub only sends our two hostnames, so ask the client directly with a made-up OPEN host
        from l0wproto import tunnel as lt

        from l0w_tunnel.client import TunnelClient

        sent = []

        class FakeWs:
            async def send(self, data):
                sent.append(lt.decode(data))
        c = TunnelClient("ws://x", None, {"a.example": "http://127.0.0.1:1"})
        c._ws = FakeWs()
        from l0w_tunnel.client import _Stream
        s = _Stream()
        s.body.put_nowait(None)
        await c._handle(1, s, {"method": "GET", "target": "/", "host": "Evil.Example", "headers": [], "client": "1.1.1.1", "scheme": "https"})
        assert sent[0].json()["status"] == 421 and sent[-1].type == lt.END
        # a target that refuses connections is a 502
        sent.clear()
        s = _Stream()
        s.body.put_nowait(None)
        c.http = httpx.AsyncClient(timeout=None)
        await c._handle(3, s, {"method": "GET", "target": "/", "host": "A.example", "headers": [], "client": "1.1.1.1", "scheme": "https"})
        assert sent[0].json()["status"] == 502
        await c.http.aclose()
    run_service(body)


def test_streamed_response_arrives_incrementally(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        async with rig.visit("instance") as c:
            t0 = time.monotonic()
            times = []
            async with c.stream("GET", "/stream") as r:
                async for chunk in r.aiter_bytes():
                    times.append((time.monotonic() - t0, chunk))
            assert b"".join(ch for _, ch in times) == b"chunk0\nchunk1\nchunk2\nchunk3\n"
            assert times[0][0] < 1.0 and times[-1][0] > 1.0  # first chunk long before the last
            assert len(times) >= 3
    run_service(body)


def test_large_upload_obeys_flow_control(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        async with rig.visit("instance") as c:
            r = await c.post("/up", content=b"x" * 900_000)
            assert r.json()["len"] == 900_000
        async with rig.visit("files") as c:
            r = await c.post("/up", content=b"y" * 262_145)
            assert r.json()["len"] == 262_145
    run_service(body)


def test_status_reports_and_config_removal(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        assert rig.gw.states[:2] == ["connecting", "connected"]
        assert rig.gw.statuses[1]["session"]
        rig.gw.config = None
        await until(lambda: rig.gw.states[-1] == "idle")
        async with rig.visit("instance") as c:
            r = await c.get("/", headers={"accept": "application/json"})
            assert r.status_code == 502 and r.json()["error"] == "instance_offline"
        rig.gw.config = rig.config()
        await until(lambda: rig.gw.states[-1] == "connected")
    run_service(body)


def test_no_config_is_idle(rig, run_service):
    rig.gw.config = None

    async def body(svc):
        await until(lambda: rig.gw.states == ["idle"])
    run_service(body)


def test_hub_restart_reconnects(rig, run_service):
    async def body(svc):
        await until(lambda: "connected" in rig.gw.states)
        rig.hub.stop()
        await until(lambda: "offline" in rig.gw.states)
        rig.hub = Hub(rig.hub.tmp, rig.hub.port).start()
        await until(lambda: rig.gw.states[-1] == "connected")
        async with rig.visit("instance") as c:
            assert (await c.get("/again")).json()["target"] == "control"
    run_service(body)


def test_wrong_key_is_refused(rig, run_service):
    rig.gw.signer = keys.generate("sign")

    async def body(svc):
        await until(lambda: "refused" in rig.gw.states)
        s = next(x for x in rig.gw.statuses if x["state"] == "refused")
        assert s["error"] == "handshake refused" and s["close_code"] == 4401
    run_service(body)
