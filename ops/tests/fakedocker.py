"""A fake Docker Engine API on a unix socket. It records every request and answers just what ops-docker should ask."""

import asyncio
import json
import threading

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

SELF = "0123456789ab"
PROJECT = "l0wagent"
WORKDIR = "/srv/my l0w/l0wagent"
NEW_ID = "f" * 64
OLD_ID = "a" * 64


class FakeDocker:
    def __init__(self, path: str):
        self.path = path
        self.requests: list[tuple] = []
        self.labels = {"com.docker.compose.project": PROJECT, "com.docker.compose.project.working_dir": WORKDIR}
        self.updaters: list[dict] = []        # containers carrying l0w.updater=1
        self.pull_errors: dict[str, str] = {}  # image -> error text inside the stream
        self.create_status = 201
        self._server: uvicorn.Server | None = None

    async def handle(self, request: Request) -> Response:
        body = (await request.body()) or b""
        self.requests.append((request.method, request.url.path, dict(request.query_params), json.loads(body) if body else None))
        p, m = request.url.path, request.method
        if m == "GET" and p == f"/containers/{SELF}/json":
            return JSONResponse({"Id": SELF + "0" * 52, "Config": {"Labels": self.labels}})
        if m == "GET" and p == "/containers/json":
            flt = json.loads(request.query_params.get("filters", "{}"))
            rows = [{"Id": "b" * 64, "Names": ["/l0wagent-control-1"], "Image": "ghcr.io/cyrusbrown/l0wagent-control:0.9.0", "State": "running", "Status": "Up",
                     "Labels": {"com.docker.compose.service": "control", "org.opencontainers.image.version": "0.9.0"}}]
            if any(x.startswith("l0w.updater") for x in flt.get("label", [])):
                rows = self.updaters
            return JSONResponse(rows)
        if m == "POST" and p == "/images/create":
            image = request.query_params["fromImage"]

            async def stream():
                yield json.dumps({"status": "Pulling from x"}).encode() + b"\n"
                if image in self.pull_errors:
                    yield json.dumps({"errorDetail": {"message": self.pull_errors[image]}, "error": self.pull_errors[image]}).encode() + b"\n"
                else:
                    yield json.dumps({"status": "Download complete"}).encode() + b"\n"
            return StreamingResponse(stream(), media_type="application/json")
        if m == "DELETE" and p.startswith("/containers/"):
            return Response(status_code=204)
        if m == "POST" and p == "/containers/create":
            return JSONResponse({"Id": NEW_ID}, status_code=self.create_status) if self.create_status < 400 else JSONResponse({"message": "No such image"}, status_code=self.create_status)
        if m == "POST" and p == f"/containers/{NEW_ID}/start":
            return Response(status_code=204)
        return JSONResponse({"message": "unexpected request to the fake docker"}, status_code=418)

    def start(self) -> None:
        app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST", "DELETE", "PUT", "PATCH"])])
        self._server = uvicorn.Server(uvicorn.Config(app, uds=self.path, log_level="error"))
        t = threading.Thread(target=lambda: asyncio.run(self._server.serve()), daemon=True)
        t.start()
        import time
        for _ in range(100):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake docker didn't start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
