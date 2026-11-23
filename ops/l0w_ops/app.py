"""The `ops` service: control's fixed command API (see README.md). No Docker access; updates go through ops-docker."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from functools import wraps

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import extensions
from .common import OpsError, check_tag, check_token, error_response, json_body, need_str
from .config import Settings


class Ops:
    def __init__(self, s: Settings, docker_transport: httpx.AsyncBaseTransport | None = None, github_transport: httpx.AsyncBaseTransport | None = None):
        self.s = s
        self.install_lock = asyncio.Lock()
        self.docker_transport = docker_transport
        self.github_transport = github_transport
        self.progress: dict | None = None  # the update this process started: {version, phase, error?}
        self._task: asyncio.Task | None = None

    # ── ops-docker ──────────────────────────────────────────────────────────
    def _docker(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.s.docker_url, transport=self.docker_transport, headers={"Authorization": f"Bearer {self.s.docker_token}"},
                                 timeout=httpx.Timeout(900, connect=5))

    async def docker_reachable(self) -> bool:
        if not self.s.docker_enabled:
            return False
        try:
            async with self._docker() as c:
                r = await c.get("/v0/status", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    # ── the update ──────────────────────────────────────────────────────────
    async def real_release(self, version: str) -> bool:
        """edge only on the edge channel; any other version must be a release the repository lists."""
        if version == "edge":
            return self.s.channel == "edge"
        url = f"{self.s.github_api}/repos/{self.s.github_repo}/releases/tags/v{version}"
        try:
            async with httpx.AsyncClient(transport=self.github_transport, timeout=15, headers={"Accept": "application/vnd.github+json", "User-Agent": "l0w-ops"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise OpsError(502, "release_check_failed", f"couldn't reach the release list: {e.__class__.__name__}") from e
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        raise OpsError(502, "release_check_failed", f"the release list answered {r.status_code}")

    async def start_update(self, version: str) -> dict:
        if not self.s.docker_enabled:
            raise OpsError(409, "updates_off", "updating from the web UI is off (./l0w ops enable-updates); run ./l0w update on the server")
        if self._task is not None and not self._task.done():
            raise OpsError(409, "update_running", "an update is already being started")
        if not await self.real_release(version):
            raise OpsError(422, "not_a_release", "that version isn't a published release" if version != "edge" else "edge updates only on the edge channel")
        self.progress = {"version": version, "phase": "pulling", "started_at": _now()}
        self._task = asyncio.create_task(self._run_update(version))
        return {"started": True}

    async def _run_update(self, version: str) -> None:
        try:
            async with self._docker() as c:
                r = await c.post("/v0/pull", json={"tag": version})
                _raise_for(r, "pull")
                self.progress["phase"] = "starting"
                r = await c.post("/v0/run-updater", json={"tag": version})
                _raise_for(r, "run-updater")
            self.progress["phase"] = "updater_started"
        except OpsError as e:
            self.progress.update(phase="failed", error=e.detail)
        except httpx.HTTPError as e:
            self.progress.update(phase="failed", error=f"ops-docker isn't reachable ({e.__class__.__name__})")

    def last_update(self) -> dict | None:
        try:
            v = json.loads(self.s.update_file.read_text())
        except (OSError, ValueError):
            return None
        return v if isinstance(v, dict) else None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _raise_for(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    try:
        d = r.json()
        detail = d.get("detail") or d.get("error") or r.text
    except ValueError:
        detail = r.text
    raise OpsError(502, f"docker_{what}_failed", str(detail)[:300])


def create_app(s: Settings, *, docker_transport: httpx.AsyncBaseTransport | None = None, github_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    if not s.token:
        raise ValueError("L0W_OPS_TOKEN is required")
    ops = Ops(s, docker_transport, github_transport)

    def handler(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapped(request: Request) -> JSONResponse:
            try:
                check_token(request, s.token)
                return JSONResponse(await fn(request))
            except OpsError as e:
                return error_response(e)
        return wrapped

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @handler
    async def status(request: Request) -> dict:
        return {
            "docker": await ops.docker_reachable(),
            "docker_enabled": s.docker_enabled,
            "channel": s.channel,
            "extensions": await asyncio.to_thread(extensions.installed, s),
            "update": ops.last_update(),
            "update_progress": ops.progress,
        }

    @handler
    async def resolve(request: Request) -> dict:
        body = await json_body(request)
        return await asyncio.to_thread(extensions.resolve, s, body.get("git"), body.get("ref"))

    @handler
    async def install(request: Request) -> dict:
        body = await json_body(request)
        async with ops.install_lock:
            return await asyncio.to_thread(extensions.install, s, body)

    @handler
    async def remove(request: Request) -> dict:
        body = await json_body(request)
        async with ops.install_lock:
            return await asyncio.to_thread(extensions.remove, s, body)

    @handler
    async def update(request: Request) -> dict:
        body = await json_body(request)
        version = check_tag(need_str(body, "version"))
        return await ops.start_update(version)

    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "not_found", "detail": "no such endpoint"}, status_code=404)

    app = Starlette(routes=[
        Route("/healthz", healthz),
        Route("/v0/status", status),
        Route("/v0/extensions/resolve", resolve, methods=["POST"]),
        Route("/v0/extensions/install", install, methods=["POST"]),
        Route("/v0/extensions/remove", remove, methods=["POST"]),
        Route("/v0/update", update, methods=["POST"]),
    ], exception_handlers={404: not_found})
    app.state.ops = ops
    return app
