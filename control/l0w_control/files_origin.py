"""The files origin: agent-made files rendered in the browser, on a separate origin.

Control serves this second app on its own port (L0W_FILES_PORT, default 8001). The UI puts
HTML from the agent's storage in a sandboxed frame pointing here, never at the main origin,
so nothing the agent wrote can run with the owner's session. (Images, SVG shown as an <img>,
media and PDFs drawn client-side are inert, so the UI uses those from the main origin's raw routes.)
- No cookies: access is a short-lived signed link (`sign()`), scoped to one file, one
  folder (so an HTML page's relative assets load) or one shelf item.
- Every response carries `Content-Security-Policy: sandbox …`, errors included (`SandboxHeaders`
  wraps the whole app, outside FastAPI's error handling) and the UI's frames add their own `sandbox` attribute, so pages get an
  opaque origin with no access to anything else.
- On a hub the files app is reached by its own hostname rather than a port: control's main
  app hands every request for the files hostname (`L0W_FILES_ORIGIN`) to this app
  (`HostDispatch`), so nothing else of control answers there.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .backends.base import BadRequest, NotFound
from .files import Storage, clean, guess_mime

TTL_S = 3600
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
CSP = f"{SANDBOX}; default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval'; base-uri 'none'; form-action 'none'; frame-ancestors *"
RANGE_MAX = 8 * 1024 * 1024


class Signer:
    def __init__(self, key: bytes):
        self.key = hashlib.sha256(b"l0wagent files origin\0" + key).digest()

    def sign(self, scope: dict, ttl: int = TTL_S) -> str:
        body = base64.urlsafe_b64encode(json.dumps({**scope, "e": int(time.time()) + ttl}, separators=(",", ":")).encode()).decode().rstrip("=")
        mac = base64.urlsafe_b64encode(hmac.new(self.key, body.encode(), hashlib.sha256).digest()[:18]).decode()
        return f"{body}.{mac}"

    def verify(self, token: str) -> dict:
        body, _, mac = token.partition(".")
        good = base64.urlsafe_b64encode(hmac.new(self.key, body.encode(), hashlib.sha256).digest()[:18]).decode()
        if not mac or not hmac.compare_digest(mac, good):
            raise NotFound("link")
        scope = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if scope.get("e", 0) < time.time():
            raise NotFound("link (expired)")
        return scope


def _headers(mime: str) -> dict:
    h = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "cross-origin",
         "Access-Control-Allow-Origin": "*", "Cache-Control": "private, max-age=300", "Accept-Ranges": "bytes"}
    h["Content-Security-Policy"] = CSP
    return h


class SandboxHeaders:
    """ASGI wrapper: the files app's security headers on every response, whatever produced it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _FORCED]
                headers += [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_FORCED = {b"content-security-policy", b"x-content-type-options", b"referrer-policy"}


def host_of(origin: str) -> str:
    """`https://files.example:8443` -> `files.example:8443` (lowercase; default ports dropped)."""
    from urllib.parse import urlsplit

    u = urlsplit(origin)
    host = (u.hostname or "").lower()
    if u.port and not ((u.scheme == "https" and u.port == 443) or (u.scheme == "http" and u.port == 80)):
        host = f"{host}:{u.port}"
    return host


class HostDispatch:
    """ASGI middleware for control's main app, outermost: requests whose Host is the files
    hostname go to the files app (from callers `may_reach` allows), so nothing else of control
    answers there and control's own headers (X-Frame-Options) don't stop the UI framing previews."""

    def __init__(self, app, files_app, files_host: Callable[[], str | tuple[str, ...]], may_reach: Callable[[str | None], bool]):
        self.app, self.files_app, self.files_host, self.may_reach = app, files_app, files_host, may_reach

    async def __call__(self, scope, receive, send):
        hosts = self.files_host() if scope["type"] in ("http", "websocket") else ""
        want = tuple(h for h in ((hosts,) if isinstance(hosts, str) else hosts) if h)
        if want:
            got = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "").lower()
            if got.endswith((":443", ":80")):
                got = got.rsplit(":", 1)[0]
            if got in want:
                client = scope.get("client")
                if scope["type"] == "websocket" or not self.may_reach(client[0] if client else None):
                    if scope["type"] == "websocket":
                        return await send({"type": "websocket.close", "code": 1008})
                    return await self.files_app({**scope, "path": "/forbidden", "raw_path": b"/forbidden"}, receive, send)
                return await self.files_app(scope, receive, send)
        return await self.app(scope, receive, send)


def make_app(storage: Storage, signer: Signer, shelf_raw: Callable[[int, int | None], tuple[bytes, str, str]]) -> SandboxHeaders:
    return SandboxHeaders(_make_app(storage, signer, shelf_raw))


def _make_app(storage: Storage, signer: Signer, shelf_raw: Callable[[int, int | None], tuple[bytes, str, str]]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound):
        return JSONResponse({"error": "not_found", "detail": exc.what}, status_code=404)

    @app.exception_handler(BadRequest)
    async def _br(_: Request, exc: BadRequest):
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=400)

    @app.get("/f/{token}/{rest:path}")
    def serve(token: str, rest: str, request: Request) -> Response:
        scope = signer.verify(token)
        if scope["k"] == "shelf":
            data, mime, _ = shelf_raw(int(scope["i"]), scope.get("n"))
            return Response(data, media_type=mime, headers=_headers(mime))
        if scope["k"] == "file":
            volume, path = scope["v"], scope["p"]
        elif scope["k"] == "dir":
            volume, path = scope["v"], "/".join([*clean(scope["p"]), *clean(rest)])
        else:
            raise NotFound("link")
        mime = guess_mime(path)
        rng = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            size = storage.stat(volume, path).st_size
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start, end = max(0, size - int(rng.group(2))), size - 1
            end = min(end, start + RANGE_MAX - 1, size - 1)
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            data, size = storage.read_range(volume, path, start, end)
            return Response(data, status_code=206, media_type=mime, headers={**_headers(mime), "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}"})
        body, size = storage.iter_file(volume, path)
        return StreamingResponse(body, media_type=mime, headers={**_headers(mime), "Content-Length": str(size)})

    @app.get("/{_:path}")
    def nothing_else(_: str) -> Response:
        return JSONResponse({"error": "not_found"}, status_code=404)

    return app
