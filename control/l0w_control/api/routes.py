"""Owner-facing API routes (/api/*), shared by every backend.

Agent routes (/api/agent/*), the MCP connector (/mcp) and the reserved
/api/peer/* and /api/hub/* prefixes are added in later milestones.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from starlette.background import BackgroundTask

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


@api.post("/system/update/check", tags=["system"])
def check_update(be: Be) -> s.UpdateInfo:
    return be.check_update()


# ── Files and shelf ────────────────────────────────────────────────────────
@api.get("/files/list", tags=["files"])
def list_dir(be: Be, volume: s.Volume, path: str = "") -> s.Listing:
    return be.list_dir(volume, path)


PAGE_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml")


def _raw(data: bytes, mime: str, name: str, download: bool) -> Response:
    """Bytes from the agent's storage on the main origin, never rendered as a page here:
    HTML, SVG and XML go out as text/plain (sandboxed and nosniff besides). They're shown
    only from the files origin (preview-url) in a sandboxed frame."""
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-cache"}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
        mime = "application/octet-stream"
    elif mime in PAGE_TYPES:
        mime = "text/plain; charset=utf-8"
    return Response(data, media_type=mime, headers=headers)


def _origin(request: Request, env_name: str, port_env: str, default_port: int) -> str:
    """Where the browser reaches another of the server's ports: the host of this request."""
    override = os.environ.get(env_name, "").rstrip("/")
    if override:
        return override
    host = urlsplit("//" + (request.headers.get("host") or "localhost")).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}:{os.environ.get(port_env, str(default_port))}"


@api.get("/files/raw", tags=["files"], response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def read_file(be: Be, volume: s.Volume, path: str, download: bool = False) -> Response:
    """File bytes, for downloads and client-side previewers (text, CSV, notebooks, 3D)."""
    data, mime = be.read_file(volume, path)
    return _raw(data, mime, path.rsplit("/", 1)[-1], download)


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
    return be.preview_url(volume, path, _origin(request, "L0W_FILES_ORIGIN", "L0W_FILES_PORT", 8001))


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
def shelf_raw(item_id: int, be: Be, version: int | None = None, download: bool = False) -> Response:
    """A shelf item's bytes: a kept version, the snapshot, or the live file."""
    data, mime, name = be.shelf_raw(item_id, version)
    return _raw(data, mime, name, download)


@api.get("/shelf/{item_id}/preview-url", tags=["files"])
def shelf_preview_url(item_id: int, request: Request, be: Be, version: int | None = None) -> s.PreviewUrl:
    return be.shelf_preview_url(item_id, version, _origin(request, "L0W_FILES_ORIGIN", "L0W_FILES_PORT", 8001))


@api.get("/web", tags=["files"])
def web_page(request: Request, be: Be) -> s.WebPage:
    """The assistant's web page (its port 8787, through agent-web)."""
    return be.web_page(_origin(request, "L0W_AGENT_WEB_URL", "L0W_AGENT_WEB_PORT", 8787) + "/")


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
