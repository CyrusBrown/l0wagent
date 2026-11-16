"""The request guard: who may call what, before any route runs.

- `/api/agent/*` needs the agent token (and is the only thing the sandbox may call).
- The chat-runtime sidecar's subnet may only call `/api/chat-runtime/*`, which takes a
  per-turn token (chat.py), never the owner's or the agent's credentials.
- Anything else from the sandbox subnet is refused, so the agent can never reach the
  owner API (it could approve its own requests) whatever credentials it finds.
- `/api/peer/*` and `/api/hub/*` are reserved for the hub (signed requests, later).
- Writes authenticated by the session cookie must carry `X-L0W-Client`. Cookies reach every
  port on the host, including pages the agent serves (its port 8787) and agent HTML in
  previews; a page there can't add a custom header to a cross-origin request without a CORS
  preflight, which control refuses, so it can't act as the owner (CSRF).
- Owner routes then authenticate themselves (session cookie or device token).
"""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Env

RESERVED = ("/api/peer/", "/api/hub/")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CLIENT_HEADER = "x-l0w-client"
SESSION_COOKIE = "l0w_session"


def install(app: FastAPI, env: Env) -> None:
    sandbox = ipaddress.ip_network(env.sandbox_subnet, strict=False)
    chat_net = ipaddress.ip_network(env.chat_subnet, strict=False) if env.chat_subnet else None

    def from_chat(request: Request) -> bool:
        try:
            return chat_net is not None and ipaddress.ip_address(request.client.host) in chat_net  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            return False

    def from_sandbox(request: Request) -> bool:
        try:
            return ipaddress.ip_address(request.client.host) in sandbox  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if from_chat(request):
            if path.startswith("/api/chat-runtime/"):
                return await call_next(request)
            return JSONResponse({"error": "forbidden_from_chat_runtime"}, status_code=403)
        if path.startswith("/api/chat-runtime/"):
            # With a chat subnet configured (Docker), only the sidecar may call it; without one (dev), anyone but the sandbox.
            if chat_net is not None or from_sandbox(request):
                return JSONResponse({"error": "chat_runtime_only"}, status_code=403)
            return await call_next(request)
        if path.startswith("/api/agent/") or path == "/api/agent":
            token = request.headers.get("x-agent-token", "")
            if not env.agent_token or not token or not secrets.compare_digest(token, env.agent_token):
                return JSONResponse({"error": "bad_agent_token"}, status_code=401)
            return await call_next(request)
        if from_sandbox(request):
            return JSONResponse({"error": "forbidden_from_sandbox"}, status_code=403)
        if path.startswith(RESERVED):
            return JSONResponse({"error": "reserved", "detail": "Reserved for the hub."}, status_code=404)
        if (request.method not in SAFE_METHODS and path.startswith("/api/") and SESSION_COOKIE in request.cookies
                and not request.headers.get(CLIENT_HEADER)):
            return JSONResponse({"error": "csrf", "detail": f"Writes with a session cookie need the {CLIENT_HEADER} header."}, status_code=403)
        return await call_next(request)
