"""The request guard: who may call what, before any route runs.

- Who may reach which paths is one table (`RULES`, by caller class, default deny; `allowed`).
  The class comes from the raw socket peer, never a forwarded header.
- `/api/agent/*` needs the agent token and comes only from the sandbox subnet (the token lives in
  the untrusted container, so it is useless from anywhere else). `L0W_AGENT_API_ANY_ADDRESS=1`
  lifts the address rule for dev_stack.py, where the runner is on localhost.
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
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Env

RESERVED = ("/api/peer/", "/api/hub/")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CLIENT_HEADER = "x-l0w-client"
SESSION_COOKIES = ("__Host-l0w_session", "l0w_session")  # auth.SESSION_COOKIES (not imported: guard loads first)

SANDBOX, CHAT, OTHER = "sandbox", "chat_runtime", "other"
AGENT_API, CHAT_API = "/api/agent", "/api/chat-runtime"


@dataclass(frozen=True)
class Rule:
    allow: tuple[str, ...]  # path prefixes ("/api/agent" matches itself and below)
    deny: tuple[str, ...] = ()  # carved out of `allow`


# Caller class -> what it may reach. A path no rule allows is refused.
RULES: dict[str, Rule] = {
    SANDBOX: Rule(allow=(AGENT_API,)),
    CHAT: Rule(allow=(CHAT_API,)),
    OTHER: Rule(allow=("/",), deny=(AGENT_API, CHAT_API)),
}
REFUSAL = {SANDBOX: "forbidden_from_sandbox", CHAT: "forbidden_from_chat_runtime", OTHER: "forbidden"}


def _under(path: str, prefix: str) -> bool:
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


def caller_class(host: str | None, sandbox: ipaddress.IPv4Network | ipaddress.IPv6Network, chat_net) -> str:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return OTHER
    if ip in sandbox:
        return SANDBOX
    if chat_net is not None and ip in chat_net:
        return CHAT
    return OTHER


def allowed(cls: str, path: str, *, agent_any_address: bool = False, chat_net_set: bool = True) -> bool:
    """Whether a caller of this class may reach this path, before any route runs."""
    rule = RULES[cls]
    if cls == OTHER:
        # The dev escape hatches: the runner on localhost; no chat sidecar subnet configured.
        deny = tuple(d for d in rule.deny if not ((d == AGENT_API and agent_any_address) or (d == CHAT_API and not chat_net_set)))
        rule = Rule(rule.allow, deny)
    return any(_under(path, a) for a in rule.allow) and not any(_under(path, d) for d in rule.deny)


def install(app: FastAPI, env: Env) -> None:
    sandbox = ipaddress.ip_network(env.sandbox_subnet, strict=False)
    chat_net = ipaddress.ip_network(env.chat_subnet, strict=False) if env.chat_subnet else None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        cls = caller_class(request.client.host if request.client else None, sandbox, chat_net)
        if not allowed(cls, path, agent_any_address=env.agent_api_any_address, chat_net_set=chat_net is not None):
            if cls == OTHER and _under(path, AGENT_API):
                return JSONResponse({"error": "agent_api_from_sandbox_only"}, status_code=403)
            if cls == OTHER and _under(path, CHAT_API):
                return JSONResponse({"error": "chat_runtime_only"}, status_code=403)
            return JSONResponse({"error": REFUSAL[cls]}, status_code=403)
        if _under(path, AGENT_API):
            token = request.headers.get("x-agent-token", "")
            if not env.agent_token or not token or not secrets.compare_digest(token, env.agent_token):
                return JSONResponse({"error": "bad_agent_token"}, status_code=401)
            return await call_next(request)
        if cls != OTHER or _under(path, CHAT_API):
            return await call_next(request)
        if path.startswith(RESERVED):
            return JSONResponse({"error": "reserved", "detail": "Reserved for the hub."}, status_code=404)
        if (request.method not in SAFE_METHODS and path.startswith("/api/") and any(c in request.cookies for c in SESSION_COOKIES)
                and not request.headers.get(CLIENT_HEADER)):
            return JSONResponse({"error": "csrf", "detail": f"Writes with a session cookie need the {CLIENT_HEADER} header."}, status_code=403)
        return await call_next(request)
