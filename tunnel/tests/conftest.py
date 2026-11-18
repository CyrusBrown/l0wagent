"""Test rig: a fakehub, a fake gateway and two target apps (standing in for control and files), each on its own port in a thread."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from conformance.harness import Inst, new_name
from fakehub.config import Config
from fakehub.hub import create_app
from l0wproto import docs, payloads
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from l0w_tunnel.service import Gateway, Service


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Threaded:
    def __init__(self, app, port: int | None = None):
        self.port = port or free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


class Hub:
    def __init__(self, tmp: Path, port: int | None = None):
        self.tmp = tmp
        self.port = port or free_port()
        self.cfg = Config(base_url=f"http://hub.localhost:{self.port}", state_path=tmp / "state.json", listen_port=self.port)
        self.app = create_app(self.cfg)
        self.srv = Threaded(self.app, self.port)

    def start(self):
        self.srv.start()
        return self

    def stop(self):
        self.srv.stop()

    @property
    def url(self) -> str:
        return self.cfg.base_url


def make_target(name: str) -> Starlette:
    async def echo(request: Request):
        body = await request.body()
        return JSONResponse({"target": name, "path": request.url.path, "query": request.url.query, "method": request.method, "len": len(body),
                             "headers": [[k, v] for k, v in request.headers.items()]})

    async def stream(request: Request):
        async def gen():
            for i in range(4):
                yield f"chunk{i}\n".encode()
                await asyncio.sleep(0.4)
        return StreamingResponse(gen(), media_type="text/plain")

    async def boom(request: Request):
        return Response("x", headers={"x-l0w-tunnel-extra": "1", "keep-alive": "1"})

    return Starlette(routes=[Route("/stream", stream), Route("/boom", boom), Route("/{p:path}", echo, methods=["GET", "POST", "PUT"])])


class FakeGateway:
    def __init__(self, signer):
        self.signer = signer
        self.config: dict | None = None
        self.statuses: list[dict] = []
        self.auth_seen: set[str] = set()
        self.accel_requests: list[dict] = []
        self._iat = int(time.time())
        self.app = Starlette(routes=[Route("/v0/tunnel/config", self._config), Route("/v0/tunnel/hello", self._hello, methods=["POST"]),
                                     Route("/v0/tunnel/accelerator", self._accelerator, methods=["POST"]),
                                     Route("/v0/tunnel/status", self._status, methods=["PUT"])])
        self.srv = Threaded(self.app)

    async def _config(self, request: Request):
        self.auth_seen.add(request.headers.get("authorization", ""))
        return JSONResponse({"config": self.config})

    async def _hello(self, request: Request):
        b = await request.json()
        p = payloads.build_tunnel_hello(self.config["address"], b["nonce"], b["hub"], self.config["release"])
        return JSONResponse({"doc": docs.sign_document("tunnel-hello", p, [self.signer])})

    async def _accelerator(self, request: Request):
        b = await request.json()
        self.accel_requests.append(b)
        self._iat += 1
        kw = {"service": b["service"]} if b.get("service") else {}
        p = payloads.build_accelerator_request(self.config["address"], self.config["hub"], b["action"], iat=self._iat, **kw)
        return JSONResponse({"doc": docs.sign_document("accelerator-request", p, [self.signer])})

    async def _status(self, request: Request):
        self.statuses.append(await request.json())
        return JSONResponse({"ok": True})

    @property
    def states(self) -> list[str]:
        return [s["state"] for s in self.statuses]

    @property
    def accel_states(self) -> list[str]:
        return [s["accelerator"]["state"] for s in self.statuses if "accelerator" in s]


async def until(pred, timeout: float = 15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting")


class Rig:
    def __init__(self, tmp: Path):
        self.hub = Hub(tmp).start()
        self.inst = Inst("hub.localhost", new_name())
        r = httpx.post(f"{self.hub.url}/v0/members", json=self.inst.registration(self.hub.app.hub.store.make_invite()), timeout=10)
        assert r.status_code == 201, r.text
        body = r.json()
        self.inst.hostnames = body["hostnames"]
        assert httpx.put(f"{self.hub.url}/v0/members/{self.inst.name}/self", json=self.inst.self_doc(0), timeout=10).status_code == 200
        self.tunnel_url = body["tunnel"]
        self.control = Threaded(make_target("control")).start()
        self.files = Threaded(make_target("files")).start()
        self.gw = FakeGateway(self.inst.sign)
        self.gw.srv.start()
        self.gw.config = self.config()

    def config(self) -> dict:
        return {"url": self.tunnel_url, "address": self.inst.address, "hub": "hub.localhost", "hostnames": self.inst.hostnames, "release": "0.9.0",
                "connect_to": f"127.0.0.1:{self.hub.port}"}

    def service(self) -> Service:
        gw = Gateway(f"http://127.0.0.1:{self.gw.srv.port}", "secret-token")
        return Service(gw, f"http://127.0.0.1:{self.control.port}", f"http://127.0.0.1:{self.files.port}", poll_seconds=0.2, min_delay=0.1, max_delay=0.4)

    def visit(self, which: str, **kw) -> httpx.AsyncClient:
        """An httpx client that sends to the hub's port with the member's hostname as Host."""
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.hub.port}", headers={"host": self.inst.hostnames[which]}, timeout=20, **kw)

    def close(self):
        for s in (self.gw.srv, self.control, self.files):
            s.stop()
        self.hub.stop()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


@pytest.fixture
def run_service(rig):
    """Run the service for the duration of `coro(svc)`."""
    def go(coro):
        async def main():
            svc = rig.service()
            task = asyncio.create_task(svc.run())
            try:
                await coro(svc)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await svc.aclose()
        asyncio.run(main())
    return go
