"""Owner-facing API routes (/api/*), shared by every backend.

Agent routes (/api/agent/*), the MCP connector (/mcp) and the reserved
/api/peer/* and /api/hub/* prefixes are added in later milestones.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from starlette.background import BackgroundTask

from ..auth import SECURE_SESSION_COOKIE, SESSION_COOKIE, SESSION_DAYS_DEFAULT, scope_allows  # noqa: F401
from ..backends.base import Backend
from ..membership import hub_origins
from ..netaddr import client_ip, is_lan_caller, request_scheme
from . import schemas as s


class _SessionCookie(APIKeyCookie):
    """The session cookie under either name (the Secure one is __Host- prefixed)."""

    async def __call__(self, request: Request) -> str | None:
        return request.cookies.get(SECURE_SESSION_COOKIE) or request.cookies.get(SESSION_COOKIE)


cookie_scheme = _SessionCookie(name=SESSION_COOKIE, auto_error=False, scheme_name="session", description="Browser session cookie.")
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="device", description="Device token (desktop app, l0w CLI).")


def backend(request: Request) -> Backend:
    return request.app.state.backend


Be = Annotated[Backend, Depends(backend)]


def owner(
    request: Request,
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> s.Owner:
    o = be.authenticate(cookie, bearer.credentials if bearer else None)
    if bearer and not scope_allows(be.token_scope(bearer.credentials), request.method, request.url.path):
        raise HTTPException(403, "This device token's scope doesn't allow that. Use the web app or a full-access token.")
    return o


public = APIRouter(prefix="/api")
# Unauthenticated documents at fixed paths (the hub protocol's self-description).
wellknown = APIRouter(include_in_schema=False)


@wellknown.get("/.well-known/l0wagent/self")
def self_description(be: Be) -> Response:
    """The current signed self-description (protocol/identity.md), once this instance is a member of a hub."""
    doc = be.self_description()
    if doc is None:
        return JSONResponse({"v": "l0w/0.1", "error": "not_a_member", "detail": "This instance isn't a member of a hub."}, status_code=404)
    return JSONResponse(doc, headers={"Cache-Control": "no-cache"})
api = APIRouter(prefix="/api", dependencies=[Depends(owner)])


def _secure(request: Request, be: Backend) -> bool:
    mode = (getattr(getattr(be, "env", None), "cookie_secure", "auto") or "auto").lower()
    return mode == "always" or (mode != "never" and request_scheme(request) == "https")


def _set_session(request: Request, response: Response, be: Backend, token: str | None) -> None:
    if token:
        secure = _secure(request, be)
        days = be.settings.get("SESSION_DAYS") if hasattr(be, "settings") else SESSION_DAYS_DEFAULT
        response.set_cookie(SECURE_SESSION_COOKIE if secure else SESSION_COOKIE, token, max_age=days * 86400, httponly=True, samesite="lax", secure=secure, path="/")
        if secure:
            response.delete_cookie(SESSION_COOKIE, path="/")  # a leftover plain cookie from before HTTPS


def _client(request: Request) -> tuple[str, str]:
    return (client_ip(request), request.headers.get("user-agent", ""))


# ── Public: meta, login, setup ─────────────────────────────────────────────
@public.get("/meta", tags=["meta"])
def get_meta(be: Be) -> s.Meta:
    return be.meta()


LOGIN_ERRORS = {401: {"model": s.LoginError}, 429: {"model": s.LoginError}, 423: {"model": s.LoginError}}


@public.post("/auth/login", tags=["auth"], responses=LOGIN_ERRORS)
def login(req: s.LoginRequest, request: Request, response: Response, be: Be) -> s.LoginResult:
    result, cookie = be.login(req, *_client(request))
    _set_session(request, response, be, cookie)
    return result


@public.post("/auth/totp", tags=["auth"], responses=LOGIN_ERRORS)
def login_totp(req: s.TotpRequest, request: Request, response: Response, be: Be) -> s.LoginResult:
    result, cookie = be.login_totp(req, *_client(request))
    _set_session(request, response, be, cookie)
    return result


@public.post("/auth/logout", tags=["auth"])
def logout(
    response: Response,
    be: Be,
    cookie: Annotated[str | None, Depends(cookie_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> s.Ok:
    be.logout(cookie, bearer.credentials if bearer else None)
    for name in (SESSION_COOKIE, SECURE_SESSION_COOKIE):
        response.delete_cookie(name, path="/", secure=name == SECURE_SESSION_COOKIE, httponly=True, samesite="lax")
    return s.Ok()


@public.post("/setup/owner", tags=["auth"], responses={400: {"model": s.PasswordProblem}, 409: {"model": s.PasswordProblem}})
def setup_owner(req: s.SetupOwnerRequest, request: Request, be: Be) -> s.Owner:
    """First-run wizard step 1. Refused once an owner exists. Needs the setup code ./l0w install
    printed; with none configured (dev stacks), only a local or private-network caller may."""
    code = getattr(getattr(be, "env", None), "setup_code", "")
    if code:
        if not hmac.compare_digest((req.setup_code or "").encode(), code.encode()):
            raise HTTPException(403, "That setup code is wrong. ./l0w install printed it (it's L0W_SETUP_CODE in .env).")
    elif not is_lan_caller(request):
        raise HTTPException(403, "Set L0W_SETUP_CODE (./l0w install does) to set up from outside your network.")
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


@api.post("/account/devices", tags=["auth"], status_code=201)
def create_device(req: s.DeviceCreate, request: Request, be: Be) -> s.DeviceCreated:
    """A new device token (for the `l0w` CLI, a script, another app), shown once; revoke it under sessions."""
    return be.create_device(req, client_ip(request), request.headers.get("user-agent", ""))


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
def totp_enable(req: s.TotpEnable, be: Be) -> s.TotpEnabled:
    """Verifies against the secret from /totp/setup and returns one-time recovery codes (shown once)."""
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


# ── Budgets, providers, roles, the reviewer (online only) ─────────────────
@api.get("/budget", tags=["budgets"])
def budget_overview(be: Be) -> s.BudgetOverview:
    """Per provider: level, Boost, limits, allowance left, you vs. the assistant, the reply budget."""
    return be.budget_overview()


@api.patch("/budget/{provider}", tags=["budgets"])
def patch_budget(provider: str, req: s.BudgetPatch, be: Be) -> s.ProviderBudget:
    """Set the level (clears overrides) or override single limits."""
    return be.patch_budget(provider, req)


@api.post("/budget/{provider}/boost", tags=["budgets"])
def boost(provider: str, req: s.BoostRequest, be: Be) -> s.ProviderBudget:
    return be.boost(provider, req)


@api.delete("/budget/{provider}/boost", tags=["budgets"])
def clear_boost(provider: str, be: Be) -> s.ProviderBudget:
    return be.clear_boost(provider)


@api.patch("/budget", tags=["budgets"])
def patch_reserve(req: s.ReserveSettings, be: Be) -> s.BudgetOverview:
    """The owner's reserve and the sweep's window and ceiling."""
    return be.patch_reserve(req)


@api.get("/providers", tags=["budgets"])
def list_providers(be: Be) -> list[s.ProviderInfo]:
    return be.providers()


@api.post("/providers", tags=["budgets"])
def add_key_provider(req: s.ApiProviderCreate, be: Be) -> s.ProviderInfo:
    """Save a provider key: an Anthropic API key or an opencode Go key (write-only; only
    sessions on that provider get it). `use_for_all_roles` moves every role onto it."""
    return be.add_api_provider(req)


@api.post("/providers/anthropic-api", tags=["budgets"])
def add_api_provider(req: s.ApiProviderCreate, be: Be) -> s.ProviderInfo:
    """Save an Anthropic API key (write-only; only sessions on this provider get it)."""
    return be.add_api_provider(req.model_copy(update={"kind": "anthropic-api"}))


@api.post("/providers/claude-subscription/login", tags=["budgets"])
def claude_login_start(be: Be) -> s.ClaudeLoginStart:
    """Start logging the assistant's computer in to a Claude subscription: returns the sign-in link."""
    return be.claude_login_start()


@api.post("/providers/claude-subscription/login/code", tags=["budgets"])
def claude_login_code(req: s.ClaudeLoginCode, be: Be) -> s.ClaudeLoginResult:
    """Finish the login with the code Claude showed after signing in."""
    return be.claude_login_code(req)


@api.get("/providers/{provider}/catalog", tags=["budgets"])
def provider_catalog(provider: str, be: Be, refresh: bool = False) -> list[s.ProviderModel]:
    """opencode providers: every model the catalog offers (with tool use), prices, and which are enabled."""
    return be.provider_catalog(provider, refresh)


@api.patch("/providers/{provider}", tags=["budgets"])
def patch_provider(provider: str, req: s.ProviderPatch, be: Be) -> s.ProviderInfo:
    return be.patch_provider(provider, req)


@api.delete("/providers/{provider}", tags=["budgets"])
def delete_provider(provider: str, be: Be) -> s.Ok:
    be.delete_provider(provider)
    return s.Ok()


@api.get("/roles", tags=["budgets"])
def get_roles(be: Be) -> s.Roles:
    return be.roles()


@api.put("/roles", tags=["budgets"])
def set_roles(req: s.Roles, be: Be) -> s.Roles:
    return be.set_roles(req)


@api.get("/reviewer", tags=["budgets"])
def get_reviewer(be: Be) -> s.ReviewerConfig:
    return be.reviewer_config()


@api.put("/reviewer", tags=["budgets"])
def set_reviewer(req: s.ReviewerConfig, be: Be) -> s.ReviewerConfig:
    return be.set_reviewer_config(req)


@api.get("/instructions", tags=["budgets"])
def get_instructions(be: Be) -> s.StandingInstructions:
    """Standing instructions: included in every brief."""
    return be.instructions()


@api.get("/settings/advanced", tags=["settings"])
def get_advanced_settings(be: Be) -> list[s.AdvancedSetting]:
    """Tuning knobs without a screen of their own (Settings → Advanced)."""
    return be.advanced_settings()


@api.patch("/settings/advanced", tags=["settings"])
def set_advanced_settings(req: s.AdvancedSettingsUpdate, be: Be) -> list[s.AdvancedSetting]:
    return be.set_advanced_settings(req.values)


@api.put("/instructions", tags=["budgets"])
def set_instructions(req: s.StandingInstructions, be: Be) -> s.StandingInstructions:
    return be.set_instructions(req)


# ── Calendar, wakeups, routines ────────────────────────────────────────────
@api.get("/calendar", tags=["calendar"])
def calendar(be: Be, start: str | None = None, end: str | None = None) -> s.Calendar:
    """Planned wakeups, routine occurrences, allowed hours and the sweep window (default: this week, Monday 00:00 local)."""
    return be.calendar(start, end)


@api.post("/wakeups", tags=["calendar"])
def create_wakeup(req: s.WakeupCreate, be: Be) -> s.Wakeup:
    """Your own wakeup. Online only."""
    return be.create_wakeup(req)


@api.patch("/wakeups/{wakeup_id}", tags=["calendar"])
def patch_wakeup(wakeup_id: int, req: s.WakeupPatch, be: Be) -> s.Wakeup:
    """Move or resize a planned wakeup (the assistant hears about changes to its own)."""
    return be.patch_wakeup(wakeup_id, req)


@api.delete("/wakeups/{wakeup_id}", tags=["calendar"])
def cancel_wakeup(wakeup_id: int, be: Be) -> s.Wakeup:
    return be.cancel_wakeup(wakeup_id)


@api.get("/routines", tags=["calendar"])
def list_routines(be: Be) -> list[s.Routine]:
    return be.routines_list()


@api.post("/routines", tags=["calendar"])
def create_routine(req: s.RoutineCreate, be: Be) -> s.Routine:
    return be.create_routine(req)


@api.patch("/routines/{routine_id}", tags=["calendar"])
def patch_routine(routine_id: int, req: s.RoutinePatch, be: Be) -> s.Routine:
    return be.patch_routine(routine_id, req)


@api.delete("/routines/{routine_id}", tags=["calendar"])
def delete_routine(routine_id: int, be: Be) -> s.Ok:
    be.delete_routine(routine_id)
    return s.Ok()


# ── 1:1 chats (M6) ─────────────────────────────────────────────────────────
@api.get("/chats", tags=["chats"])
def list_chats(be: Be, archived: bool = False) -> list[s.Chat]:
    return be.chats(archived)


@api.post("/chats", tags=["chats"], status_code=201)
def create_chat(req: s.ChatCreate, be: Be) -> s.Chat:
    """Start a chat with its first message; the reply streams as `chat_delta` live events."""
    return be.create_chat(req)


@api.get("/chats/{chat_id}", tags=["chats"])
def get_chat(chat_id: int, be: Be) -> s.Chat:
    return be.chat(chat_id)


@api.patch("/chats/{chat_id}", tags=["chats"])
def patch_chat(chat_id: int, req: s.ChatPatch, be: Be) -> s.Chat:
    return be.patch_chat(chat_id, req)


@api.get("/chats/{chat_id}/messages", tags=["chats"])
def chat_messages(chat_id: int, be: Be) -> list[s.ChatMessage]:
    return be.chat_messages(chat_id)


@api.post("/chats/{chat_id}/messages", tags=["chats"], status_code=201)
def send_chat_message(chat_id: int, req: s.ChatMessageCreate, be: Be) -> s.ChatMessage:
    return be.send_chat_message(chat_id, req)


@api.post("/chats/{chat_id}/stop", tags=["chats"])
def stop_chat(chat_id: int, be: Be) -> s.Chat:
    return be.stop_chat(chat_id)


@api.post("/chats/{chat_id}/cards/{message_id}", tags=["chats"])
def decide_chat_card(chat_id: int, message_id: int, req: s.ChatCardDecision, be: Be) -> s.ChatMessage:
    """Act on a card the chat proposed. Online only. A secret's value is stored write-only."""
    return be.decide_chat_card(chat_id, message_id, req)


@api.post("/chats/{chat_id}/handoff", tags=["chats"])
def chat_handoff(chat_id: int, req: s.ChatHandoff, be: Be) -> s.Chat:
    """Move to the assistant: a new thread with the summary and the transcript attached."""
    return be.chat_handoff(chat_id, req)


@api.post("/chats/{chat_id}/ask", tags=["chats"])
def chat_ask(chat_id: int, req: s.ChatAsk, be: Be) -> s.ChatMessage:
    return be.chat_ask(chat_id, req)


@api.post("/chats/{chat_id}/context", tags=["chats"])
def chat_pull(chat_id: int, req: s.ChatPull, be: Be) -> s.Chat:
    """Pull a thread into the chat as read-only context."""
    return be.chat_pull(chat_id, req)


@api.post("/chat-files", tags=["chats"], status_code=201)
async def upload_chat_file(be: Be, file: Annotated[UploadFile, File()]) -> s.ChatFile:
    """A file for a 1:1 chat message (up to 25 MB). It stays in control, not the agent's storage;
    send it with the message's upload_ids."""
    return be.upload_chat_file(file.filename or "file", await file.read(), file.content_type)


@api.get("/chat-files/{file_id}", tags=["chats"], response_class=Response)
def chat_file(file_id: int, be: Be) -> Response:
    """Download a chat file (always as an attachment, never rendered on this origin)."""
    data, name, mime = be.chat_file(file_id)
    return Response(data, media_type="application/octet-stream" if mime in ("text/html", "image/svg+xml") else mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "sandbox; default-src 'none'"})


# ── Network ────────────────────────────────────────────────────────────────
@api.get("/network", tags=["network"])
def network(be: Be, filter: Literal["all", "blocked", "get", "post", "hour"] = "all") -> s.NetworkView:
    """Egress rules, the request log (newest first, up to 200, filtered) and the last day's busiest hosts."""
    return be.network(filter)


@api.post("/network/rules", tags=["network"])
def add_egress_rule(req: s.EgressRuleCreate, be: Be) -> s.EgressRule:
    """Online only. Blocking a model provider the assistant runs on is refused."""
    return be.add_egress_rule(req)


@api.delete("/network/rules/{rule_id}", tags=["network"])
def delete_egress_rule(rule_id: int, be: Be) -> s.Ok:
    be.delete_egress_rule(rule_id)
    return s.Ok()


# ── Integrations ───────────────────────────────────────────────────────────
@api.get("/integrations", tags=["integrations"])
def list_integrations(be: Be) -> list[s.Integration]:
    return be.list_integrations()


@api.get("/integrations/presets", tags=["integrations"])
def integration_presets(be: Be) -> list[s.IntegrationPreset]:
    return be.integration_presets()


@api.post("/integrations", tags=["integrations"], status_code=201)
def create_integration(req: s.IntegrationCreate, be: Be) -> s.Integration:
    """Online only. Connects to the server and lists its tools when the token (if any) is already saved."""
    return be.create_integration(req)


@api.get("/integrations/{name}", tags=["integrations"])
def get_integration(name: str, be: Be) -> s.Integration:
    return be.get_integration(name)


@api.patch("/integrations/{name}", tags=["integrations"])
def update_integration(name: str, req: s.IntegrationPatch, be: Be) -> s.Integration:
    return be.update_integration(name, req)


@api.delete("/integrations/{name}", tags=["integrations"])
def delete_integration(name: str, be: Be) -> s.Ok:
    be.delete_integration(name)
    return s.Ok()


@api.post("/integrations/{name}/sync", tags=["integrations"])
def sync_integration(name: str, be: Be) -> s.Integration:
    """Ask the server for its tools again. New tools get the default policy; existing policies stay."""
    return be.sync_integration(name)


@api.put("/integrations/{name}/tools/{tool}", tags=["integrations"])
def set_integration_tool_policy(name: str, tool: str, req: s.ToolPolicySet, be: Be) -> s.Integration:
    return be.set_integration_tool_policy(name, tool, req.policy)


# ── The MCP connector: the consent screen and connected apps ───────────────
@api.get("/connector/request", tags=["connector"])
def connect_request(request: Request, be: Be) -> s.ConnectRequest:
    """Describe an OAuth authorization request (its query parameters, as /oauth/authorize got them)."""
    return be.connect_request(dict(request.query_params))


@api.post("/connector/consent", tags=["connector"])
def connect_consent(req: s.ConnectConsent, be: Be) -> s.ConnectRedirect:
    return be.connect_consent(req)


@api.get("/connected-apps", tags=["connector"])
def connected_apps(be: Be) -> list[s.ConnectedApp]:
    return be.connected_apps()


@api.delete("/connected-apps/{app_id}", tags=["connector"])
def revoke_connected_app(app_id: int, be: Be) -> s.Ok:
    be.revoke_connected_app(app_id)
    return s.Ok()


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


@api.get("/notifications/settings", tags=["notifications"])
def notification_settings(be: Be) -> s.NotificationSettings:
    return be.notification_settings()


@api.put("/notifications/settings", tags=["notifications"])
def set_notification_settings(req: s.NotificationSettings, be: Be) -> s.NotificationSettings:
    """Online only."""
    return be.set_notification_settings(req)


@api.get("/push/key", tags=["notifications"])
def push_key(be: Be) -> s.PushKey:
    return be.push_key()


@api.post("/push/subscriptions", tags=["notifications"])
def push_subscribe(req: s.PushSubscribe, be: Be) -> s.NotificationSettings:
    return be.push_subscribe(req)


@api.delete("/push/subscriptions/{sub_id}", tags=["notifications"])
def push_unsubscribe(sub_id: int, be: Be) -> s.Ok:
    be.push_unsubscribe(sub_id)
    return s.Ok()


@api.post("/push/test", tags=["notifications"])
def push_test(be: Be) -> s.PushTestResult:
    return be.push_test()


@api.get("/system/update", tags=["system"])
def update_info(be: Be) -> s.UpdateInfo:
    """Whether a newer release exists (apply it with ./l0w update), and whether one is waiting for idle."""
    return be.update_info()


@api.get("/membership", tags=["hub"])
def membership(be: Be) -> s.Membership:
    """This instance's hub membership: none (standalone), joined, or left."""
    return be.membership()


@api.post("/membership/preview", tags=["hub"])
def membership_preview(req: s.InvitePreviewRequest, be: Be) -> s.InvitePreview:
    """Online only. Reads an invite: fetches and verifies the hub's capabilities, checks the invite, and lists what would stop a join."""
    return be.membership_preview(req)


@api.post("/membership/join", tags=["hub"])
def membership_join(req: s.JoinRequest, be: Be) -> s.JoinResult:
    """Online only. Joins the hub (needs two-step sign-in). The gateway makes fresh keys; the recovery key is in the answer, once."""
    return be.membership_join(req)


@api.post("/membership/check", tags=["hub"])
def membership_check(be: Be) -> s.MembershipCheck:
    """Runs the self-check now (it also runs daily)."""
    return be.membership_check()


@api.put("/membership/accelerator", tags=["hub"])
def membership_accelerator(req: s.AcceleratorRequest, be: Be) -> s.Membership:
    """Allow or refuse the hub's faster connection (cloudflared) when it offers one."""
    return be.membership_accelerator(req)


@api.post("/membership/rotate", tags=["hub"])
def membership_rotate(be: Be) -> s.Membership:
    """Replaces the signing key (the old key signs the rotation) and publishes a new self-description."""
    return be.membership_rotate()


@api.post("/membership/recover", tags=["hub"])
def membership_recover(req: s.RecoverRequest, be: Be) -> s.Membership:
    """Replaces the signing key with a rotation signed by the recovery key (when the signing key is lost or stolen)."""
    return be.membership_recover(req)


@api.post("/membership/leave", tags=["hub"])
def membership_leave(req: s.LeaveRequest, be: Be) -> s.Membership:
    """Leaves the hub. The name is never given to anyone else, and can't be had back."""
    return be.membership_leave(req)


@api.post("/system/update/check", tags=["system"])
def check_update(be: Be) -> s.UpdateInfo:
    return be.check_update()


@api.post("/system/update/cancel", tags=["system"])
def cancel_update(be: Be) -> s.UpdateInfo:
    """Clear a pending update's drain, so runs can start again (a stuck ./l0w update)."""
    return be.cancel_update()


@api.get("/system/pause", tags=["system"])
def pause_state(be: Be) -> s.PauseState:
    return be.pause_state()


@api.put("/system/pause", tags=["system"])
def pause(req: s.PauseRequest, be: Be) -> s.PauseState:
    """Online only. The emergency pause: stops the current run, refuses new ones and skips due wakeups
    until it ends or you resume. The chat and the connector keep working."""
    return be.pause(req)


@api.delete("/system/pause", tags=["system"])
def resume(be: Be) -> s.PauseState:
    """End the pause now. The assistant is told which wakeups it skipped."""
    return be.resume()


# ── Files and shelf ────────────────────────────────────────────────────────
@api.get("/files/list", tags=["files"])
def list_dir(be: Be, volume: s.Volume, path: str = "") -> s.Listing:
    return be.list_dir(volume, path)


PAGE_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml")


RANGE_MAX = 8 * 1024 * 1024


def _range(header: str | None, size: int) -> tuple[int, int] | None:
    """One `bytes=a-b`, `a-` or `-n` range as inclusive (start, end), capped at 8 MiB; None if
    there's no usable header (serve it whole); 416 if it lies outside the file."""
    m = re.match(r"bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        n = int(m.group(2))
        start, end = max(0, size - n), size - 1
        if n == 0:
            start = size
    if start >= size or end < start:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})
    return start, min(end, start + RANGE_MAX - 1, size - 1)


def _raw(data, mime: str, name: str, download: bool, size: int | None = None) -> Response:
    """Bytes from the agent's storage on the main origin, never rendered as a page here:
    HTML, SVG and XML go out as text/plain (sandboxed and nosniff besides). HTML is shown
    only from the files origin (preview-url) in a sandboxed frame; images, SVG (as an <img>),
    media and PDFs (drawn client-side) are used from here and stay inert."""
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-cache", "Accept-Ranges": "bytes"}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
        mime = "application/octet-stream"
    elif mime in PAGE_TYPES:
        mime = "text/plain; charset=utf-8"
    if size is None:
        return Response(data, media_type=mime, headers=headers)
    return StreamingResponse(data, media_type=mime, headers={**headers, "Content-Length": str(size)})


def _raw_range(data: bytes, mime: str, name: str, download: bool, rng: tuple[int, int], size: int) -> Response:
    r = _raw(data, mime, name, download)
    r.status_code = 206
    r.headers["Content-Range"] = f"bytes {rng[0]}-{rng[0] + len(r.body) - 1}/{size}"
    return r


def _origin(request: Request, env_name: str, port_env: str, default_port: int) -> str:
    """Where the browser reaches another of the server's ports: the host of this request."""
    if env_name == "L0W_FILES_ORIGIN":  # reached through a hub: the files hostname it assigned
        hub_files = hub_origins(request)[1]
        if hub_files:
            return hub_files
    override = os.environ.get(env_name, "").rstrip("/")
    if override:
        return override
    host = urlsplit("//" + (request.headers.get("host") or "localhost")).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    scheme = request_scheme(request)
    port = os.environ.get(port_env) or (os.environ.get("L0W_FILES_PORT") if port_env == "L0W_FILES_PUBLIC_PORT" else None) or str(default_port)
    return f"{scheme}://{host}:{port}"


@api.get("/files/raw", tags=["files"], response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def read_file(request: Request, be: Be, volume: s.Volume, path: str, download: bool = False) -> Response:
    """File bytes, for downloads, media elements (Range supported) and client-side previewers."""
    if request.headers.get("range"):
        size, mime = be.file_stat(volume, path)
        rng = _range(request.headers["range"], size)
        if rng:
            return _raw_range(be.file_range(volume, path, *rng), mime, path.rsplit("/", 1)[-1], download, rng, size)
    body, size, mime = be.stream_file(volume, path)
    return _raw(body, mime, path.rsplit("/", 1)[-1], download, size)


@api.get("/files/zip", tags=["files"], response_class=Response, responses={200: {"content": {"application/zip": {}}}})
def zip_dir(be: Be, volume: s.Volume, path: str = "") -> Response:
    """A folder as a zip (symlinks skipped)."""
    f = be.zip_dir(volume, path)
    name = (path.rstrip("/").rsplit("/", 1)[-1] or volume) + ".zip"
    return StreamingResponse(iter(lambda: f.read(1 << 20), b""), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}, background=BackgroundTask(f.close))


@api.get("/files/preview-url", tags=["files"])
def file_preview_url(request: Request, be: Be, volume: s.Volume, path: str) -> s.PreviewUrl:
    """A link on the files origin for showing this file in a frame or media element. HTML
    gets a link scoped to its folder, so its relative assets load."""
    return be.preview_url(volume, path, _origin(request, "L0W_FILES_ORIGIN", "L0W_FILES_PUBLIC_PORT", 8001))


@api.get("/files/diff", tags=["files"])
def file_diff(be: Be, volume: s.Volume, path: str, rev: str) -> s.FileDiff:
    return be.file_diff(volume, path, rev)


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


@api.get("/shelf/{item_id}/raw", tags=["files"], response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def shelf_raw(item_id: int, request: Request, be: Be, version: int | None = None, download: bool = False) -> Response:
    """A shelf item's bytes: a kept version, the snapshot, or the live file (Range supported)."""
    data, mime, name = be.shelf_raw(item_id, version)
    rng = _range(request.headers.get("range"), len(data))
    if rng:
        return _raw_range(data[rng[0]:rng[1] + 1], mime, name, download, rng, len(data))
    return _raw(data, mime, name, download)


@api.get("/shelf/{item_id}/preview-url", tags=["files"])
def shelf_preview_url(item_id: int, request: Request, be: Be, version: int | None = None) -> s.PreviewUrl:
    return be.shelf_preview_url(item_id, version, _origin(request, "L0W_FILES_ORIGIN", "L0W_FILES_PUBLIC_PORT", 8001))


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


# ── extensions (M8) ─────────────────────────────────────────────────────────
@api.get("/extensions", tags=["extensions"])
def list_extensions(be: Be) -> list[s.ExtensionInfo]:
    """Everything found under extensions/, on or off, with errors. An extension's own routes are at /api/ext/<name>/."""
    return be.extensions()


@api.post("/extensions/rescan", tags=["extensions"])
def rescan_extensions(be: Be) -> list[s.ExtensionInfo]:
    """Read the extensions folder again (after adding one or pulling a new version). Enabled ones reload."""
    return be.rescan_extensions()


@api.put("/extensions/{name}/enabled", tags=["extensions"])
def set_extension_enabled(name: str, req: s.ExtensionEnable, be: Be) -> s.ExtensionInfo:
    return be.set_extension_enabled(name, req.enabled)


@api.put("/extensions/{name}/settings", tags=["extensions"])
def set_extension_settings(name: str, req: s.ExtensionSettingsUpdate, be: Be) -> s.ExtensionInfo:
    return be.set_extension_settings(name, req.values)


@api.get("/extensions/{name}/ui/{path:path}", include_in_schema=False)
def extension_ui(name: str, path: str, be: Be) -> Response:
    """The extension's UI bundle (its module's folder), for the web app to import."""
    from fastapi.responses import FileResponse

    target = be.extension_ui_file(name, path)
    media = "text/javascript" if target.endswith((".js", ".mjs")) else None
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@api.get("/extension-cards", tags=["extensions"])
def extension_cards(be: Be, status: Literal["open", "closed"] | None = None) -> list[s.ExtensionCard]:
    return be.extension_cards(status)


@api.post("/extension-cards/{card_id}/resolve", tags=["extensions"])
def resolve_extension_card(card_id: int, req: s.ExtensionCardResolve, be: Be) -> s.ExtensionCard:
    return be.resolve_extension_card(card_id, req)
