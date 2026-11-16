"""The connector's public endpoints: OAuth 2.1 (/oauth/*), discovery (/.well-known/*) and /mcp.

Not part of the owner's OpenAPI contract: outside MCP clients are their only callers. The owner's
side (the consent screen's calls, connected apps) is in routes.py. The guard already refuses all of
this from the sandbox.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ..backends.base import Unauthorized
from ..connector import SCOPES, OAuthError

if TYPE_CHECKING:
    from ..connector import Connector

router = APIRouter(include_in_schema=False)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _c(request: Request) -> Connector:
    return request.app.state.backend.connector


def base_url(request: Request) -> str:
    """The instance's public URL: L0W_PUBLIC_URL, else what the client used to reach it."""
    return (os.environ.get("L0W_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _oauth_error(e: OAuthError) -> JSONResponse:
    return JSONResponse({"error": e.error, "error_description": e.description}, status_code=e.status, headers=NO_STORE)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request) -> dict:
    base = base_url(request)
    return {"resource": f"{base}/mcp", "authorization_servers": [base], "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"], "resource_name": "l0wagent"}


@router.get("/.well-known/oauth-authorization-server")
def authorization_server(request: Request) -> dict:
    base = base_url(request)
    return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register", "revocation_endpoint": f"{base}/oauth/revoke",
            "scopes_supported": list(SCOPES), "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"]}


@router.post("/oauth/register")
async def register(request: Request) -> Response:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise OAuthError("invalid_client_metadata", "Send a JSON object.")
        return JSONResponse(_c(request).register(body), status_code=201, headers=NO_STORE)
    except ValueError:
        return _oauth_error(OAuthError("invalid_client_metadata", "Send a JSON object."))
    except OAuthError as e:
        return _oauth_error(e)


@router.get("/oauth/authorize")
def authorize(request: Request) -> Response:
    q = dict(request.query_params)
    try:
        _c(request).check_request(q)
    except OAuthError as e:
        # Don't redirect to an unverified URI: show the error here instead.
        return JSONResponse({"error": e.error, "error_description": e.description}, status_code=400)
    return RedirectResponse(f"/connect?{urlencode(q)}", status_code=302)


@router.post("/oauth/token")
async def token(request: Request) -> Response:
    form = dict(await request.form())
    try:
        return JSONResponse(_c(request).token({k: str(v) for k, v in form.items()}), headers=NO_STORE)
    except OAuthError as e:
        return _oauth_error(e)


@router.post("/oauth/revoke")
async def revoke(request: Request) -> Response:
    form = dict(await request.form())
    _c(request).revoke_token(str(form.get("token") or ""))
    return Response(status_code=200)


def _unauthorized(request: Request) -> Response:
    meta = f"{base_url(request)}/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "invalid_token"}, status_code=401,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


@router.post("/mcp")
async def mcp(request: Request) -> Response:
    c = _c(request)
    try:
        grant = c.authenticate(request.headers.get("authorization"))
    except Unauthorized:
        return _unauthorized(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    base = base_url(request)

    def handle() -> list | dict | None:
        if isinstance(body, list):
            return [r for m in body[:50] if (r := c.handle(grant, m, base)) is not None] or None
        return c.handle(grant, body, base)

    res = await run_in_threadpool(handle)
    return Response(status_code=202) if res is None else JSONResponse(res)


@router.get("/mcp")
def mcp_stream(request: Request) -> Response:
    if not request.headers.get("authorization"):
        return _unauthorized(request)
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
def mcp_end() -> Response:
    return Response(status_code=204)


@router.get("/connector/skill.zip")
def skill_zip() -> Response:
    """The skill that teaches an outside model how to use the connector (a folder with SKILL.md)."""
    import io
    import zipfile
    from pathlib import Path

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("l0wagent/SKILL.md", (Path(__file__).parent.parent / "connector_skill" / "SKILL.md").read_text())
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="l0wagent-skill.zip"'})


# ── the chat-runtime sidecar's tools (a token per turn; the guard limits who may call) ──
@router.post("/api/chat-runtime/mcp")
async def chat_runtime_mcp(request: Request) -> Response:
    chats = request.app.state.backend.chats_
    chat_id = chats.turn_chat(request.headers.get("authorization"))
    if chat_id is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    def handle() -> list | dict | None:
        if isinstance(body, list):
            return [r for m in body[:50] if (r := chats.mcp(chat_id, m)) is not None] or None
        return chats.mcp(chat_id, body)

    res = await run_in_threadpool(handle)
    return Response(status_code=202) if res is None else JSONResponse(res)


@router.delete("/api/chat-runtime/mcp")
def chat_runtime_mcp_end() -> Response:
    return Response(status_code=204)
