"""The `ops-docker` service: three fixed commands on the Docker socket.

This is not a socket proxy. It never forwards anything it receives: each command builds its own Docker API requests from a
validated tag and from what it reads about its own container, so nothing that reaches it can ask Docker for anything else.

    GET  /v0/status       this compose project's containers
    POST /v0/pull         {tag}: pull the project's images at that tag
    POST /v0/run-updater  {tag}: start one container from the ops image that runs `l0w-updater TAG` in the project folder

It runs as root in its container (the socket is root's) with every capability dropped, no network but `opsnet`, a read-only
filesystem, and only the socket mounted. The socket is root-equivalent on the host, which is why this is opt-in
(`./l0w ops enable-updates`) and why the service is this small. Called only by `ops`, with its own token.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .common import OpsError, check_tag, check_token, error_response, json_body, need_str

REGISTRY = "ghcr.io/cyrusbrown/l0wagent"
IMAGES = [f"{REGISTRY}-{n}" for n in ("control", "agent", "egress", "chat-runtime", "gateway", "tunnel", "ops")]
OPS_IMAGE = f"{REGISTRY}-ops"
SOCKET = "/var/run/docker.sock"
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
# Where the project lives on the host. Used in a bind spec ("src:dst"), so no colons, and nothing exotic.
HOSTPATH_RE = re.compile(r"^/[A-Za-z0-9_./@+=,~ -]{0,500}\Z")
ID_RE = re.compile(r"^[0-9a-f]{12,64}\Z")
UPDATER_LABEL = "l0w.updater"
ACTIVE_STATES = {"running", "restarting", "paused"}


class DockerAPI:
    """The only code that talks to the socket; each method is one fixed request."""

    def __init__(self, path: str = SOCKET, transport: httpx.AsyncBaseTransport | None = None):
        self.path = path
        self.transport = transport

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        transport = self.transport or httpx.AsyncHTTPTransport(uds=self.path)
        return httpx.AsyncClient(base_url="http://docker", transport=transport, timeout=timeout)

    async def inspect(self, container_id: str) -> dict:
        if not ID_RE.match(container_id) and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", container_id):
            raise OpsError(500, "bad_self", "this container's id doesn't look right")
        async with self._client() as c:
            r = await c.get(f"/containers/{container_id}/json")
        return _ok(r, "inspect")

    async def list_project(self, project: str, *, extra_label: str | None = None) -> list[dict]:
        labels = [f"com.docker.compose.project={project}"] + ([extra_label] if extra_label else [])
        async with self._client() as c:
            r = await c.get("/containers/json", params={"all": "1", "filters": json.dumps({"label": labels})})
        return _ok(r, "list")

    async def pull(self, image: str, tag: str) -> str | None:
        """Pull one image; returns an error text or None. Docker streams progress as JSON lines and reports failure inside the stream."""
        async with self._client(timeout=httpx.Timeout(900, connect=5)) as c, c.stream("POST", "/images/create", params={"fromImage": image, "tag": tag}) as r:
            if r.status_code != 200:
                return (await r.aread()).decode(errors="replace")[:300] or f"HTTP {r.status_code}"
            async for line in r.aiter_lines():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("error"):
                    return str(msg["error"])[:300]
        return None

    async def remove(self, container_id: str) -> None:
        if not ID_RE.match(container_id):
            raise OpsError(500, "bad_id", "unexpected container id")
        async with self._client() as c:
            r = await c.delete(f"/containers/{container_id}", params={"v": "1"})
        if r.status_code not in (204, 404):
            _ok(r, "remove")

    async def create(self, name: str, body: dict) -> str:
        async with self._client() as c:
            r = await c.post("/containers/create", params={"name": name}, json=body)
        return _ok(r, "create")["Id"]

    async def start(self, container_id: str) -> None:
        if not ID_RE.match(container_id):
            raise OpsError(500, "bad_id", "unexpected container id")
        async with self._client() as c:
            r = await c.post(f"/containers/{container_id}/start")
        if r.status_code not in (204, 304):
            _ok(r, "start")


def _ok(r: httpx.Response, what: str):
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise OpsError(502, f"docker_{what}", f"Docker said: {str(msg)[:300]}")
    return r.json()


async def project_of_self(api: DockerAPI, self_id: str) -> tuple[str, str]:
    """(compose project, the project folder's path on the host) from this container's own labels."""
    info = await api.inspect(self_id)
    labels = (info.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    if not PROJECT_RE.match(project):
        raise OpsError(500, "no_project", "this container isn't part of a compose project")
    if not HOSTPATH_RE.match(workdir) or ".." in workdir.split("/"):
        raise OpsError(500, "no_project_dir", "can't tell where the project folder is on the host")
    return project, workdir


def updater_body(tag: str, project: str, workdir: str) -> dict:
    """The one container this service ever creates. Fixed except for the tag, the project and its folder."""
    return {
        "Image": f"{OPS_IMAGE}:{tag}",
        "Cmd": ["l0w-updater", tag],
        "User": "0:0",
        "WorkingDir": workdir,
        "Env": ["L0W_IN_UPDATER=1"],
        # oneoff + service keep `docker compose up --remove-orphans` (run by the updater itself) from removing this container.
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": "l0w-updater",
            "com.docker.compose.oneoff": "True",
            UPDATER_LABEL: "1",
        },
        "HostConfig": {
            "Binds": [f"{SOCKET}:{SOCKET}", f"{workdir}:{workdir}"],
            "AutoRemove": False,
            "Privileged": False,
            "RestartPolicy": {"Name": "no"},
            "SecurityOpt": ["no-new-privileges:true"],
        },
    }


def create_app(token: str, *, api: DockerAPI | None = None, self_id: str | None = None) -> Starlette:
    if not token:
        raise ValueError("L0W_OPS_DOCKER_TOKEN is required")
    api = api or DockerAPI()
    me = self_id or os.environ.get("OPS_DOCKER_SELF") or socket.gethostname()
    busy = asyncio.Lock()

    def handler(fn):
        async def wrapped(request: Request) -> JSONResponse:
            try:
                check_token(request, token)
                return JSONResponse(await fn(request))
            except OpsError as e:
                return error_response(e)
        return wrapped

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @handler
    async def status(request: Request) -> dict:
        project, _ = await project_of_self(api, me)
        rows = await api.list_project(project)
        return {"project": project, "containers": [{
            "name": ((c.get("Names") or ["?"])[0]).lstrip("/"),
            "service": (c.get("Labels") or {}).get("com.docker.compose.service"),
            "image": c.get("Image"), "state": c.get("State"), "status": c.get("Status"),
            "version": (c.get("Labels") or {}).get("org.opencontainers.image.version"),
        } for c in rows]}

    @handler
    async def pull(request: Request) -> dict:
        tag = check_tag(need_str(await json_body(request), "tag"))
        async with busy:
            results = {}
            for image in IMAGES:
                results[image] = await api.pull(image, tag)
        if results[OPS_IMAGE]:  # the updater runs from this one; the others are checked again (and kept if already here) by `./l0w update`
            raise OpsError(502, "pull_failed", f"couldn't pull {OPS_IMAGE}:{tag}: {results[OPS_IMAGE]}")
        return {"pulled": [i for i, e in results.items() if not e], "failed": {i: e for i, e in results.items() if e}}

    @handler
    async def run_updater(request: Request) -> dict:
        tag = check_tag(need_str(await json_body(request), "tag"))
        async with busy:
            project, workdir = await project_of_self(api, me)
            previous = await api.list_project(project, extra_label=f"{UPDATER_LABEL}=1")
            if any(c.get("State") in ACTIVE_STATES for c in previous):
                raise OpsError(409, "update_running", "an updater is already running")
            for c in previous:
                await api.remove(c["Id"])
            cid = await api.create(f"{project}-updater", updater_body(tag, project, workdir))
            await api.start(cid)
        return {"started": True}

    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "not_found", "detail": "no such endpoint"}, status_code=404)

    return Starlette(routes=[
        Route("/healthz", healthz),
        Route("/v0/status", status),
        Route("/v0/pull", pull, methods=["POST"]),
        Route("/v0/run-updater", run_updater, methods=["POST"]),
    ], exception_handlers={404: not_found})


def main() -> None:
    try:
        app = create_app(os.environ.get("L0W_OPS_DOCKER_TOKEN", ""))
    except ValueError as e:
        sys.exit(f"ops-docker: {e}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OPS_DOCKER_PORT", "8796")), access_log=False)


if __name__ == "__main__":
    main()
