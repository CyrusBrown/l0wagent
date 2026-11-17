"""The control app factory.

    uvicorn l0w_control.app:app            backend from L0W_BACKEND (default: sqlite, the real one)
    L0W_BACKEND=mock uvicorn ...           in-memory fixtures for UI work
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from . import __version__, guard
from .api import schemas
from .api.agent import router as agent_router
from .api.connector_routes import router as connector_router
from .api.routes import api, public
from .backends.base import Backend, BadRequest, Conflict, LoginFailed, NotFound, Unauthorized

API_VERSION = "0"

# The desktop app's local origins (Tauri). A Vite dev server sets L0W_CORS_ORIGINS itself: a page on a
# localhost port is the same site as control there and must not get the owner's cookie by default.
DEFAULT_ORIGINS = ["tauri://localhost", "http://tauri.localhost"]


def create_app(backend: Backend) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await backend.startup()
        try:
            yield
        finally:
            await backend.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        title="l0wagent control",
        version=f"{API_VERSION}.{__version__}",
        description="The owner-facing API. The agent's routes (/api/agent/*) and the connector's (/mcp, /oauth/*) aren't part of it.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        separate_input_output_schemas=False,  # one schema per model, so generated TS types are shared
    )
    app.state.backend = backend
    ext = getattr(backend, "ext", None)
    if ext is not None:  # extensions' own routes; the guard and the owner/agent checks still apply
        from starlette.routing import Mount

        ext.install_error_handlers = install_error_handlers
        app.router.routes.append(Mount("/api/agent/ext", app=ext.asgi(owner=False)))
        app.router.routes.append(Mount("/api/ext", app=ext.asgi(owner=True)))
    app.include_router(public)
    app.include_router(api)
    if hasattr(backend, "runs_mgr"):
        app.include_router(agent_router)
    if hasattr(backend, "connector"):
        app.include_router(connector_router)
    from .config import Env

    guard.install(app, getattr(backend, "env", None) or Env())

    origins = [o for o in os.environ.get("L0W_CORS_ORIGINS", "").split(",") if o] or DEFAULT_ORIGINS
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def no_framing(request: Request, call_next):
        """Other sites never frame control's pages (clickjacking, e.g. on the connector's consent screen)."""
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        return response

    install_error_handlers(app)

    def openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes, separate_input_output_schemas=False)
        # Models only referenced from non-JSON responses (the NDJSON event stream).
        components = spec.setdefault("components", {}).setdefault("schemas", {})
        live = schemas.LiveEvent.model_json_schema(ref_template="#/components/schemas/{model}")
        components["LiveEvent"] = live
        app.openapi_schema = spec
        return spec

    app.openapi = openapi  # type: ignore[method-assign]
    _serve_ui(app)
    return app


def install_error_handlers(app: FastAPI) -> None:
    """Control's errors as JSON; extensions' sub-apps get the same ones."""

    @app.exception_handler(NotImplementedError)
    async def not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "not_implemented", "detail": str(exc) or None}, status_code=501)
    
    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"error": "not_found", "detail": exc.what}, status_code=404)
    
    @app.exception_handler(Conflict)
    async def conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=409)
    
    @app.exception_handler(BadRequest)
    async def bad_request(_: Request, exc: BadRequest) -> JSONResponse:
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=400)
    
    @app.exception_handler(Unauthorized)
    async def unauthorized(_: Request, __: Unauthorized) -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    @app.exception_handler(LoginFailed)
    async def login_failed(_: Request, exc: LoginFailed) -> JSONResponse:
        headers = {"Retry-After": str(exc.problem.retry_after_s)} if exc.problem.retry_after_s else None
        return JSONResponse(exc.problem.model_dump(), status_code=exc.status, headers=headers)


def _serve_ui(app: FastAPI) -> None:
    """In the release image control also serves the web app (built into L0W_WEB_DIR):
    hashed assets cached for good, everything else (index.html, the service worker, the
    manifest) revalidated, and any other path falls back to index.html for the router."""
    from pathlib import Path

    from fastapi.responses import FileResponse

    root = Path(os.environ.get("L0W_WEB_DIR", "/app/web"))
    if not (root / "index.html").is_file():
        return
    root = root.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        if path.startswith("api/") or path == "api":
            return JSONResponse({"error": "not_found", "detail": path}, status_code=404)
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            immutable = path.startswith("assets/")
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


def _default_backend() -> Backend:
    kind = os.environ.get("L0W_BACKEND", "sqlite")
    if kind == "mock":
        from .mock.backend import MockBackend

        return MockBackend(require_login=os.environ.get("L0W_MOCK_LOGIN") == "1")
    if kind == "sqlite":
        from .backends.sqlite import SqliteBackend

        return SqliteBackend()
    raise SystemExit(f"unknown L0W_BACKEND={kind!r} (mock or sqlite)")


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """`l0w_control.app:app` for uvicorn, built on first use so importing this module
    (scripts, tests) doesn't open a database."""
    global _app
    if name != "app":
        raise AttributeError(name)
    if _app is None:
        import logging

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        _app = create_app(_default_backend())
    return _app
