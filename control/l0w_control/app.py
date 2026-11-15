"""The control app factory.

    uvicorn l0w_control.app:app            backend from L0W_BACKEND (default: mock until M1)
    L0W_BACKEND=mock uvicorn ...           in-memory fixtures for UI work
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from . import __version__
from .api import schemas
from .api.routes import api, public
from .backends.base import Backend, Conflict, LoginFailed, NotFound, Unauthorized

API_VERSION = "0"

# The desktop app's local origin (Tauri) plus the Vite dev server.
DEFAULT_ORIGINS = ["tauri://localhost", "http://tauri.localhost", "http://localhost:5173"]


def create_app(backend: Backend) -> FastAPI:
    app = FastAPI(
        title="l0wagent control",
        version=f"{API_VERSION}.{__version__}",
        description="The owner-facing API. Agent routes (/api/agent/*) and /mcp are separate and come later.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        separate_input_output_schemas=False,  # one schema per model, so generated TS types are shared
    )
    app.state.backend = backend
    app.include_router(public)
    app.include_router(api)

    origins = [o for o in os.environ.get("L0W_CORS_ORIGINS", "").split(",") if o] or DEFAULT_ORIGINS
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(NotImplementedError)
    async def not_implemented(_: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse({"error": "not_implemented", "detail": str(exc) or None}, status_code=501)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"error": "not_found", "detail": exc.what}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=409)

    @app.exception_handler(Unauthorized)
    async def unauthorized(_: Request, __: Unauthorized) -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.exception_handler(LoginFailed)
    async def login_failed(_: Request, exc: LoginFailed) -> JSONResponse:
        headers = {"Retry-After": str(exc.problem.retry_after_s)} if exc.problem.retry_after_s else None
        return JSONResponse(exc.problem.model_dump(), status_code=exc.status, headers=headers)

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
    return app


def _default_backend() -> Backend:
    kind = os.environ.get("L0W_BACKEND", "mock")
    if kind == "mock":
        from .mock.backend import MockBackend

        return MockBackend(require_login=os.environ.get("L0W_MOCK_LOGIN") == "1")
    raise SystemExit(f"unknown L0W_BACKEND={kind!r} (only 'mock' exists before M1)")


app = create_app(_default_backend())
