"""Owner-facing API routes (/api/*), shared by every backend.

Agent routes (/api/agent/*), the MCP connector (/mcp) and the reserved
/api/peer/* and /api/hub/* prefixes are added in later milestones.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from ..backends.base import Backend
from . import schemas as s

SESSION_COOKIE = "l0w_session"

cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, scheme_name="session", description="Browser session cookie.")
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="device", description="Device token (desktop app, l0w CLI).")


def backend(request: Request) -> Backend:
    return request.app.state.backend


Be = Annotated[Backend, Depends(backend)]


def owner(
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> s.Owner:
    return be.authenticate(cookie, bearer.credentials if bearer else None)


public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(owner)])


def _set_session(response: Response, token: str | None) -> None:
    if token:
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False, path="/")


def _client(request: Request) -> tuple[str, str]:
    return (request.client.host if request.client else "unknown", request.headers.get("user-agent", ""))


# ── Public: meta, login, setup ─────────────────────────────────────────────
@public.get("/meta", tags=["meta"])
def get_meta(be: Be) -> s.Meta:
    return be.meta()


LOGIN_ERRORS = {401: {"model": s.LoginError}, 429: {"model": s.LoginError}, 423: {"model": s.LoginError}}


@public.post("/auth/login", tags=["auth"], responses=LOGIN_ERRORS)
def login(req: s.LoginRequest, request: Request, response: Response, be: Be) -> s.LoginResult:
    result, cookie = be.login(req, *_client(request))
    _set_session(response, cookie)
    return result


@public.post("/auth/totp", tags=["auth"], responses=LOGIN_ERRORS)
def login_totp(req: s.TotpRequest, request: Request, response: Response, be: Be) -> s.LoginResult:
    result, cookie = be.login_totp(req, *_client(request))
    _set_session(response, cookie)
    return result


@public.post("/auth/logout", tags=["auth"])
def logout(
    response: Response,
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> s.Ok:
    be.logout(cookie, bearer.credentials if bearer else None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return s.Ok()


@public.post("/setup/owner", tags=["auth"], responses={400: {"model": s.PasswordProblem}, 409: {"model": s.PasswordProblem}})
def setup_owner(req: s.SetupOwnerRequest, be: Be) -> s.Owner:
    """First-run wizard step 1. Refused once an owner exists."""
    return be.setup_owner(req)


# ── Account ────────────────────────────────────────────────────────────────
@api.get("/auth/me", tags=["auth"])
def me(o: Annotated[s.Owner, Depends(owner)]) -> s.Owner:
    return o


@api.get("/account/sessions", tags=["auth"])
def list_sessions(
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[s.AuthSession]:
    return be.auth_sessions(cookie, bearer.credentials if bearer else None)


@api.delete("/account/sessions/{session_id}", tags=["auth"])
def revoke_session(session_id: str, be: Be) -> s.Ok:
    be.revoke_session(session_id)
    return s.Ok()


@api.post("/account/password", tags=["auth"], responses={400: {"model": s.PasswordProblem}})
def change_password(
    req: s.PasswordChange,
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> s.Ok:
    """Online only. Signs out every other session and device."""
    be.change_password(req, cookie, bearer.credentials if bearer else None)
    return s.Ok()


@api.post("/account/totp/setup", tags=["auth"])
def totp_setup(be: Be) -> s.TotpSetup:
    """A new secret to scan. Nothing changes until it's enabled with a valid code."""
    return be.totp_setup()


@api.post("/account/totp/enable", tags=["auth"], responses={400: {"model": s.LoginError}})
def totp_enable(req: s.TotpEnable, be: Be) -> s.Owner:
    return be.totp_enable(req)


@api.post("/account/totp/disable", tags=["auth"], responses={400: {"model": s.PasswordProblem}})
def totp_disable(req: s.TotpDisable, be: Be) -> s.Owner:
    return be.totp_disable(req)


# ── Status, sync, live events ──────────────────────────────────────────────
@api.get("/status", tags=["meta"])
def get_status(be: Be) -> s.Status:
    return be.status()


@api.get("/sync", tags=["meta"])
def sync(be: Be, since: str | None = None) -> s.SyncResponse:
    """Changes since a cursor, for the offline cache. Omit `since` for a full snapshot."""
    return be.sync(since)


@api.get(
    "/events",
    tags=["meta"],
    response_class=StreamingResponse,
    responses={200: {"description": "Newline-delimited LiveEvent objects.", "content": {"application/x-ndjson": {"schema": {"$ref": "#/components/schemas/LiveEvent"}}}}},
)
async def events(be: Be) -> StreamingResponse:
    """Live stream, read with fetch() so apps can send a token header. One JSON object per line."""

    async def lines():
        async for event in be.events():
            yield json.dumps(event.model_dump(mode="json")) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ── Projects ───────────────────────────────────────────────────────────────
@api.get("/projects", tags=["projects"])
def list_projects(be: Be, status: s.ProjectStatus | None = None) -> list[s.Project]:
    return be.projects(status)


@api.post("/projects", tags=["projects"], status_code=201)
def create_project(req: s.ProjectCreate, be: Be) -> s.Project:
    return be.create_project(req)


@api.get("/projects/{slug}", tags=["projects"])
def get_project(slug: str, be: Be) -> s.Project:
    return be.project(slug)


@api.patch("/projects/{slug}", tags=["projects"])
def patch_project(slug: str, req: s.ProjectPatch, be: Be) -> s.Project:
    return be.patch_project(slug, req)


# ── Threads and messages ───────────────────────────────────────────────────
@api.get("/threads", tags=["threads"])
def list_threads(
    be: Be,
    project: Annotated[str | None, Query(description="Project slug.")] = None,
    general: Annotated[bool, Query(description="Only threads without a project.")] = False,
    archived: bool = False,
) -> list[s.Thread]:
    return be.threads(project, general, archived)


@api.post("/threads", tags=["threads"], status_code=201)
def create_thread(req: s.ThreadCreate, be: Be) -> s.Thread:
    return be.create_thread(req)


@api.get("/threads/{thread_id}", tags=["threads"])
def get_thread(thread_id: int, be: Be) -> s.Thread:
    return be.thread(thread_id)


@api.patch("/threads/{thread_id}", tags=["threads"])
def patch_thread(thread_id: int, req: s.ThreadPatch, be: Be) -> s.Thread:
    return be.patch_thread(thread_id, req)


@api.get("/threads/{thread_id}/items", tags=["threads"])
def thread_items(thread_id: int, be: Be, before: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> s.ThreadItems:
    """Messages and cards (asks, claims, suggestions, promote requests) in time order."""
    return be.thread_items(thread_id, before, limit)


@api.post("/threads/{thread_id}/messages", tags=["threads"], status_code=201)
def post_message(thread_id: int, req: s.MessageCreate, be: Be) -> s.Message:
    return be.post_message(thread_id, req)


@api.get("/messages/recent", tags=["threads"])
def recent_messages(be: Be, sender: s.Sender | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[s.RecentMessage]:
    """Latest messages across all threads (Home's "recent from the assistant")."""
    return be.recent_messages(sender, limit)


@api.post("/threads/{thread_id}/read", tags=["threads"])
def mark_read(thread_id: int, be: Be, message_id: int | None = None) -> s.Ok:
    be.mark_read(thread_id, message_id)
    return s.Ok()


@api.get("/threads/{thread_id}/delivery", tags=["threads"])
def get_delivery(thread_id: int, be: Be) -> s.Delivery:
    return be.delivery(thread_id)


@api.post("/threads/{thread_id}/reply-now", tags=["threads"])
def reply_now(thread_id: int, be: Be) -> s.Run:
    """Start a short reply run on the reply budget. Online only."""
    return be.reply_now(thread_id)


# ── Inbox and cards ────────────────────────────────────────────────────────
@api.get("/inbox", tags=["inbox"])
def inbox(
    be: Be,
    view: Literal["waiting", "history"] = "waiting",
    kind: s.InboxKind | None = None,
    q: str | None = None,
) -> list[s.InboxEntry]:
    return be.inbox(view, kind, q)


@api.get("/inbox/counts", tags=["inbox"])
def inbox_counts(be: Be) -> s.InboxCounts:
    return be.inbox_counts()


@api.post("/questions/{qid}/answer", tags=["inbox"])
def answer_question(qid: int, req: s.QuestionAnswer, be: Be) -> s.Question:
    return be.answer_question(qid, req)


@api.post("/permissions/{pid}/decide", tags=["inbox"])
def decide_permission(pid: int, req: s.PermissionDecision, be: Be) -> s.Permission:
    return be.decide_permission(pid, req)


@api.post("/tasks/{tid}/resolve", tags=["inbox"])
def resolve_task(tid: int, req: s.TaskResolve, be: Be) -> s.Task:
    return be.resolve_task(tid, req)


@api.post("/suggestions/{sid}/decide", tags=["inbox"])
def decide_suggestion(sid: int, req: s.SuggestionDecision, be: Be) -> s.Suggestion:
    return be.decide_suggestion(sid, req)


@api.post("/suggestions/accept-all", tags=["inbox"])
def accept_all_suggestions(be: Be) -> list[s.Suggestion]:
    return be.accept_all_suggestions()


@api.post("/promote-requests/{pid}/decide", tags=["inbox"])
def decide_promote(pid: int, req: s.PromoteDecision, be: Be) -> s.PromoteRequest:
    return be.decide_promote(pid, req)


@api.post("/security-alerts/{aid}/decide", tags=["inbox"])
def decide_security(aid: int, req: s.SecurityDecision, be: Be) -> s.SecurityAlert:
    return be.decide_security(aid, req)


@api.get("/claims", tags=["threads"])
def list_claims(be: Be, active: bool = True) -> list[s.Claim]:
    return be.claims(active)


# ── Search ─────────────────────────────────────────────────────────────────
@api.get("/search", tags=["search"])
def search(
    be: Be,
    q: Annotated[str, Query(min_length=1)],
    kind: s.SearchKind | None = None,
    project: str | None = None,
    since: Annotated[str | None, Query(description="ISO date")] = None,
) -> list[s.SearchHit]:
    return be.search(q, kind, project, since)


# ── Runs and jobs ──────────────────────────────────────────────────────────
@api.get("/runs", tags=["runs"])
def list_runs(be: Be, kind: s.RunKind | None = None, project: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[s.Run]:
    return be.runs(kind, project, limit)


@api.post("/runs", tags=["runs"], status_code=201)
def start_run(req: s.RunStart, be: Be) -> s.Run:
    """Start a manual wakeup. Online only."""
    return be.start_run(req)


@api.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: int, be: Be) -> s.RunDetail:
    return be.run(run_id)


@api.get("/runs/{run_id}/timeline", tags=["runs"])
def run_timeline(
    run_id: int,
    be: Be,
    view: Literal["simple", "detailed"] = "simple",
    kind: Annotated[list[s.TimelineKind] | None, Query()] = None,
    after: Annotated[int | None, Query(description="Only items with a larger id.")] = None,
) -> list[s.TimelineItem]:
    return be.timeline(run_id, view, kind, after)


@api.post("/runs/{run_id}/stop", tags=["runs"])
def stop_run(run_id: int, be: Be) -> s.Run:
    return be.stop_run(run_id)


@api.get("/jobs", tags=["runs"])
def list_jobs(be: Be, include_done: bool = False) -> list[s.Job]:
    return be.jobs(include_done)


# ── Secrets ────────────────────────────────────────────────────────────────
@api.get("/secrets", tags=["secrets"])
def list_secrets(be: Be) -> list[s.SecretInfo]:
    return be.secrets()


@api.put("/secrets/{name}", tags=["secrets"], responses={400: {"model": s.PasswordProblem}})
def set_secret(name: str, req: s.SecretSet, be: Be) -> s.SecretInfo:
    """Write-only: the value goes to the agent's environment and is redacted everywhere else."""
    return be.set_secret(name, req)


@api.delete("/secrets/{name}", tags=["secrets"])
def delete_secret(name: str, be: Be) -> s.Ok:
    be.delete_secret(name)
    return s.Ok()


# ── System ─────────────────────────────────────────────────────────────────
@api.get("/system", tags=["system"])
def system_status(be: Be) -> s.SystemStatus:
    """The agent computer, egress, usage and whether runs may start."""
    return be.system_status()


@api.post("/system/selftest", tags=["system"])
def selftest(be: Be) -> s.SelfTestResult:
    """Run the isolation self-test (also shown at the end of the setup wizard)."""
    return be.selftest()


# ── Files and shelf ────────────────────────────────────────────────────────
@api.get("/files/list", tags=["files"])
def list_dir(be: Be, volume: s.Volume, path: str = "") -> s.Listing:
    return be.list_dir(volume, path)


@api.get("/files/raw", tags=["files"], response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def read_file(be: Be, volume: s.Volume, path: str) -> Response:
    """File bytes. Agent-made HTML/SVG is never rendered from this origin: previews load it
    from the separate files origin in a sandboxed frame (M3)."""
    data, mime = be.read_file(volume, path)
    if mime in ("text/html", "image/svg+xml"):
        mime = "text/plain; charset=utf-8"
    return Response(data, media_type=mime, headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


@api.get("/files/history", tags=["files"])
def file_history(be: Be, volume: s.Volume, path: str) -> list[s.FileHistoryEntry]:
    return be.file_history(volume, path)


@api.get("/projects/{slug}/shelf", tags=["files"])
def get_shelf(slug: str, be: Be) -> list[s.ShelfItem]:
    return be.shelf(slug)


@api.post("/projects/{slug}/shelf", tags=["files"], status_code=201)
def add_to_shelf(slug: str, req: s.ShelfCreate, be: Be) -> s.ShelfItem:
    return be.add_to_shelf(slug, req, "owner")


@api.delete("/shelf/{item_id}", tags=["files"])
def remove_from_shelf(item_id: int, be: Be) -> s.Ok:
    be.remove_from_shelf(item_id)
    return s.Ok()


@api.get("/shelf/{item_id}/versions", tags=["files"])
def shelf_versions(item_id: int, be: Be) -> list[s.ShelfVersion]:
    return be.shelf_versions(item_id)



@api.post("/uploads", tags=["files"], status_code=201)
async def upload(
    be: Be,
    file: Annotated[UploadFile, File()],
    project: Annotated[str | None, Form(description="Project slug; omit for General (/work/_inbox/).")] = None,
    idempotency_key: Annotated[str | None, Form()] = None,
) -> s.Upload:
    """The only way control writes into the agent's storage: into an _inbox/ folder."""
    data = await file.read()
    return be.upload(project, file.filename or "upload", data, file.content_type, idempotency_key)
