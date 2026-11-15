"""The request guard: who may call what, before any route runs.

- `/api/agent/*` needs the agent token (and is the only thing the sandbox may call).
- Anything else from the sandbox subnet is refused, so the agent can never reach the
  owner API (it could approve its own requests) whatever credentials it finds.
- `/api/peer/*` and `/api/hub/*` are reserved for the hub (signed requests, later).
- Owner routes then authenticate themselves (session cookie or device token).
"""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Env

RESERVED = ("/api/peer/", "/api/hub/")


def install(app: FastAPI, env: Env) -> None:
    sandbox = ipaddress.ip_network(env.sandbox_subnet, strict=False)

    def from_sandbox(request: Request) -> bool:
        try:
            return ipaddress.ip_address(request.client.host) in sandbox  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/agent/") or path == "/api/agent":
            token = request.headers.get("x-agent-token", "")
            if not env.agent_token or not token or not secrets.compare_digest(token, env.agent_token):
                return JSONResponse({"error": "bad_agent_token"}, status_code=401)
            return await call_next(request)
        if from_sandbox(request):
            return JSONResponse({"error": "forbidden_from_sandbox"}, status_code=403)
        if path.startswith(RESERVED):
            return JSONResponse({"error": "reserved", "detail": "Reserved for the hub."}, status_code=404)
        return await call_next(request)
