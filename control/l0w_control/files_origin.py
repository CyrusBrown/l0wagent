"""The files origin: agent-made files rendered in the browser, on a separate origin.

Control serves this second app on its own port (L0W_FILES_PORT, default 8001). The UI puts
HTML, SVG, PDFs, images and media from the agent's storage in frames and media elements
pointing here, never at the main origin, so nothing the agent wrote can run with the owner's
session.
- No cookies: access is a short-lived signed link (`sign()`), scoped to one file, one
  folder (so an HTML page's relative assets load) or one shelf item.
- Every response carries `Content-Security-Policy: sandbox …` (PDFs excepted: browsers won't
  render them sandboxed) and the UI's frames add their own `sandbox` attribute, so pages get
  an opaque origin with no access to anything else.
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
    if mime != "application/pdf":
        h["Content-Security-Policy"] = CSP
    return h


def make_app(storage: Storage, signer: Signer, shelf_raw: Callable[[int, int | None], tuple[bytes, str, str]]) -> FastAPI:
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
