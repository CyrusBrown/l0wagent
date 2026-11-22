"""`/api/peer/v0/*`: what other instances send this one (protocol/messages.md). Only the hub tunnel's
network may reach it (guard.py answers everyone else 404). It takes no cookie or token: the envelope is
signed, and the gateway checks it (friends.py). Not part of the owner API, so not in openapi.json."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from l0wproto.consts import MAX_ENVELOPE

router = APIRouter(prefix="/api/peer/v0", include_in_schema=False)


def _too_large() -> JSONResponse:
    return JSONResponse({"v": "l0w/0.1", "error": "too_large", "detail": f"At most {MAX_ENVELOPE // (1024 * 1024)} MiB."}, status_code=413)


@router.post("/inbox")
async def inbox(request: Request) -> JSONResponse:
    friends = getattr(request.app.state.backend, "friends_", None)
    if friends is None:
        return JSONResponse({"v": "l0w/0.1", "error": "not_here", "detail": ""}, status_code=404)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_ENVELOPE:
        return _too_large()  # refused before reading any of it
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_ENVELOPE:
            return _too_large()
        chunks.append(chunk)
    status, body, headers = await friends.receive(b"".join(chunks))
    return JSONResponse(body, status_code=status, headers=headers)
