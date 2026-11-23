"""Shared bits: errors, names, tag rules, bearer-token checks, JSON bodies."""

from __future__ import annotations

import re
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

# Same rule as the CLI (cli/l0w.py EXT_NAME) and control (extensions.NAME_RE).
EXT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
# An image tag we will pull or run: `edge`, or a release version (optionally with a suffix such as -edge.abc1234).
TAG_RE = re.compile(r"^(edge|\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?)\Z")
COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_BODY = 256 * 1024


class OpsError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def error_response(e: OpsError) -> JSONResponse:
    return JSONResponse({"error": e.code, "detail": e.detail}, status_code=e.status)


def check_token(request: Request, token: str) -> None:
    """Constant-time bearer check. An unset token never matches (the service refuses to start without one)."""
    got = request.headers.get("authorization", "")
    scheme, _, value = got.partition(" ")
    ok = scheme.lower() == "bearer" and bool(token) and secrets.compare_digest(value.strip().encode(), token.encode())
    if not ok:
        raise OpsError(401, "unauthorized", "missing or wrong token")


async def json_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise OpsError(413, "too_large", "request body too large")
    try:
        import json

        body = json.loads(raw or b"{}")
    except ValueError as e:
        raise OpsError(400, "bad_request", "body must be JSON") from e
    if not isinstance(body, dict):
        raise OpsError(400, "bad_request", "body must be a JSON object")
    return body


def need_str(body: dict, field: str, *, optional: bool = False) -> str | None:
    v = body.get(field)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v:
        raise OpsError(400, "bad_request", f"{field} must be a non-empty string")
    return v


def check_tag(tag: object) -> str:
    if not isinstance(tag, str) or not TAG_RE.match(tag) or len(tag) > 64:
        raise OpsError(400, "bad_tag", "tag must be `edge` or a release version like 0.9.1")
    return tag
