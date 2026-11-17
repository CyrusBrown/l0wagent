"""The interface routes call. One implementation per backend:

- `mock.MockBackend`: in-memory fixtures for UI work (M0)
- the SQLite backend (M1 onwards)

Methods not implemented by a backend raise NotImplementedError, which the
app turns into 501, so the contract can run ahead of the implementation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from ..api import schemas as s


class NotFound(Exception):
    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class Conflict(Exception):
    def __init__(self, error: str, detail: str = ""):
        super().__init__(error)
        self.error = error
        self.detail = detail


class BadRequest(Exception):
    def __init__(self, error: str, detail: str = ""):
        super().__init__(error)
        self.error = error
        self.detail = detail


class Unauthorized(Exception):
    pass


class LoginFailed(Exception):
    def __init__(self, problem: s.LoginError, status: int = 401):
        super().__init__(problem.error)
        self.problem = problem
        self.status = status


class Backend:
    # lifecycle: called from the app's lifespan (the event loop is running)
    async def startup(self) -> None: pass
    async def shutdown(self) -> None: pass

    # meta and status
    def meta(self) -> s.Meta: raise NotImplementedError
    def status(self) -> s.Status: raise NotImplementedError
    def sync(self, since: str | None) -> s.SyncResponse: raise NotImplementedError
    def events(self) -> AsyncIterator[s.LiveEvent]: raise NotImplementedError

    # auth: return the owner for valid credentials, else raise Unauthorized
    def authenticate(self, session_cookie: str | None, bearer: str | None) -> s.Owner: raise NotImplementedError
    def login(self, req: s.LoginRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]: raise NotImplementedError
    def login_totp(self, req: s.TotpRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]: raise NotImplementedError
    def logout(self, session_cookie: str | None, bearer: str | None) -> None: raise NotImplementedError
    def setup_owner(self, req: s.SetupOwnerRequest) -> s.Owner: raise NotImplementedError
    def auth_sessions(self, current_cookie: str | None, current_bearer: str | None) -> list[s.AuthSession]: raise NotImplementedError
    def revoke_session(self, session_id: str) -> None: raise NotImplementedError
    def change_password(self, req: s.PasswordChange, current_cookie: str | None, current_bearer: str | None) -> None: raise NotImplementedError
    def totp_setup(self) -> s.TotpSetup: raise NotImplementedError
    def totp_enable(self, req: s.TotpEnable) -> s.TotpEnabled: raise NotImplementedError
    def totp_disable(self, req: s.TotpDisable) -> s.Owner: raise NotImplementedError

    # projects and threads
    def projects(self, status: str | None) -> list[s.Project]: raise NotImplementedError
    def project(self, slug: str) -> s.Project: raise NotImplementedError
    def create_project(self, req: s.ProjectCreate) -> s.Project: raise NotImplementedError
    def patch_project(self, slug: str, req: s.ProjectPatch) -> s.Project: raise NotImplementedError
    def threads(self, project: str | None, general: bool, archived: bool) -> list[s.Thread]: raise NotImplementedError
    def thread(self, thread_id: int) -> s.Thread: raise NotImplementedError
    def create_thread(self, req: s.ThreadCreate) -> s.Thread: raise NotImplementedError
    def patch_thread(self, thread_id: int, req: s.ThreadPatch) -> s.Thread: raise NotImplementedError
    def thread_items(self, thread_id: int, before: str | None, limit: int) -> s.ThreadItems: raise NotImplementedError
    def post_message(self, thread_id: int, req: s.MessageCreate) -> s.Message: raise NotImplementedError
    def recent_messages(self, sender: str | None, limit: int) -> list[s.RecentMessage]: raise NotImplementedError
    def mark_read(self, thread_id: int, message_id: int | None) -> None: raise NotImplementedError
    def delivery(self, thread_id: int) -> s.Delivery: raise NotImplementedError
    def reply_now(self, thread_id: int) -> s.Run: raise NotImplementedError

    # asks and cards
    def inbox(self, view: str, kind: str | None, q: str | None) -> list[s.InboxEntry]: raise NotImplementedError
    def inbox_counts(self) -> s.InboxCounts: raise NotImplementedError
    def answer_question(self, qid: int, req: s.QuestionAnswer) -> s.Question: raise NotImplementedError
    def decide_permission(self, pid: int, req: s.PermissionDecision) -> s.Permission: raise NotImplementedError
    def resolve_task(self, tid: int, req: s.TaskResolve) -> s.Task: raise NotImplementedError
    def decide_suggestion(self, sid: int, req: s.SuggestionDecision) -> s.Suggestion: raise NotImplementedError
    def accept_all_suggestions(self) -> list[s.Suggestion]: raise NotImplementedError
    def decide_promote(self, pid: int, req: s.PromoteDecision) -> s.PromoteRequest: raise NotImplementedError
    def decide_security(self, aid: int, req: s.SecurityDecision) -> s.SecurityAlert: raise NotImplementedError
    def claims(self, active: bool) -> list[s.Claim]: raise NotImplementedError

    # search
    def search(self, q: str, kind: str | None, project: str | None, since: str | None) -> list[s.SearchHit]: raise NotImplementedError

    # runs
    def runs(self, kind: str | None, project: str | None, limit: int) -> list[s.Run]: raise NotImplementedError
    def run(self, run_id: int) -> s.RunDetail: raise NotImplementedError
    def timeline(self, run_id: int, view: str, kinds: list[str] | None, after: int | None) -> list[s.TimelineItem]: raise NotImplementedError
    def start_run(self, req: s.RunStart) -> s.Run: raise NotImplementedError
    def stop_run(self, run_id: int) -> s.Run: raise NotImplementedError
    def jobs(self, include_done: bool) -> list[s.Job]: raise NotImplementedError

    # budgets, providers, roles, the reviewer (M4); changes are online only
    def budget_overview(self) -> s.BudgetOverview: raise NotImplementedError
    def patch_budget(self, provider: str, req: s.BudgetPatch) -> s.ProviderBudget: raise NotImplementedError
    def boost(self, provider: str, req: s.BoostRequest) -> s.ProviderBudget: raise NotImplementedError
    def clear_boost(self, provider: str) -> s.ProviderBudget: raise NotImplementedError
    def patch_reserve(self, req: s.ReserveSettings) -> s.BudgetOverview: raise NotImplementedError
    def providers(self) -> list[s.ProviderInfo]: raise NotImplementedError
    def add_api_provider(self, req: s.ApiProviderCreate) -> s.ProviderInfo: raise NotImplementedError
    def patch_provider(self, provider: str, req: s.ProviderPatch) -> s.ProviderInfo: raise NotImplementedError
    def provider_catalog(self, provider: str, refresh: bool = False) -> list[s.ProviderModel]: raise NotImplementedError
    def delete_provider(self, provider: str) -> None: raise NotImplementedError
    def roles(self) -> s.Roles: raise NotImplementedError
    def set_roles(self, req: s.Roles) -> s.Roles: raise NotImplementedError
    def reviewer_config(self) -> s.ReviewerConfig: raise NotImplementedError
    def set_reviewer_config(self, req: s.ReviewerConfig) -> s.ReviewerConfig: raise NotImplementedError
    def instructions(self) -> s.StandingInstructions: raise NotImplementedError
    def set_instructions(self, req: s.StandingInstructions) -> s.StandingInstructions: raise NotImplementedError

    # the calendar, wakeups and routines (M4)
    def calendar(self, start: str | None, end: str | None) -> s.Calendar: raise NotImplementedError
    def create_wakeup(self, req: s.WakeupCreate) -> s.Wakeup: raise NotImplementedError
    def patch_wakeup(self, wakeup_id: int, req: s.WakeupPatch) -> s.Wakeup: raise NotImplementedError
    def cancel_wakeup(self, wakeup_id: int) -> s.Wakeup: raise NotImplementedError
    def routines_list(self) -> list[s.Routine]: raise NotImplementedError
    def create_routine(self, req: s.RoutineCreate) -> s.Routine: raise NotImplementedError
    def patch_routine(self, routine_id: int, req: s.RoutinePatch) -> s.Routine: raise NotImplementedError
    def delete_routine(self, routine_id: int) -> None: raise NotImplementedError

    # 1:1 chats (M6)
    def chats(self, archived: bool) -> list[s.Chat]: raise NotImplementedError
    def chat(self, chat_id: int) -> s.Chat: raise NotImplementedError
    def create_chat(self, req: s.ChatCreate) -> s.Chat: raise NotImplementedError
    def patch_chat(self, chat_id: int, req: s.ChatPatch) -> s.Chat: raise NotImplementedError
    def chat_messages(self, chat_id: int) -> list[s.ChatMessage]: raise NotImplementedError
    def send_chat_message(self, chat_id: int, req: s.ChatMessageCreate) -> s.ChatMessage: raise NotImplementedError
    def stop_chat(self, chat_id: int) -> s.Chat: raise NotImplementedError
    def decide_chat_card(self, chat_id: int, message_id: int, req: s.ChatCardDecision) -> s.ChatMessage: raise NotImplementedError
    def chat_handoff(self, chat_id: int, req: s.ChatHandoff) -> s.Chat: raise NotImplementedError
    def chat_ask(self, chat_id: int, req: s.ChatAsk) -> s.ChatMessage: raise NotImplementedError
    def chat_pull(self, chat_id: int, req: s.ChatPull) -> s.Chat: raise NotImplementedError
    def upload_chat_file(self, name: str, data: bytes, mime: str | None) -> s.ChatFile: raise NotImplementedError
    def chat_file(self, file_id: int) -> tuple[bytes, str, str]: raise NotImplementedError

    # network (egress rules and log; online only)
    def network(self, filter: str = "all") -> s.NetworkView: raise NotImplementedError
    def add_egress_rule(self, req: s.EgressRuleCreate) -> s.EgressRule: raise NotImplementedError
    def delete_egress_rule(self, rule_id: int) -> None: raise NotImplementedError

    # integrations (brokered MCP servers; online only)
    def list_integrations(self) -> list[s.Integration]: raise NotImplementedError
    def integration_presets(self) -> list[s.IntegrationPreset]: raise NotImplementedError
    def create_integration(self, req: s.IntegrationCreate) -> s.Integration: raise NotImplementedError
    def get_integration(self, name: str) -> s.Integration: raise NotImplementedError
    def update_integration(self, name: str, req: s.IntegrationPatch) -> s.Integration: raise NotImplementedError
    def delete_integration(self, name: str) -> None: raise NotImplementedError
    def sync_integration(self, name: str) -> s.Integration: raise NotImplementedError
    def set_integration_tool_policy(self, name: str, tool: str, policy: str) -> s.Integration: raise NotImplementedError

    # the MCP connector (consent and connected apps; online only)
    def connect_request(self, params: dict[str, str]) -> s.ConnectRequest: raise NotImplementedError
    def connect_consent(self, req: s.ConnectConsent) -> s.ConnectRedirect: raise NotImplementedError
    def connected_apps(self) -> list[s.ConnectedApp]: raise NotImplementedError
    def revoke_connected_app(self, app_id: int) -> None: raise NotImplementedError

    # secrets (write-only)
    def secrets(self) -> list[s.SecretInfo]: raise NotImplementedError
    def set_secret(self, name: str, req: s.SecretSet) -> s.SecretInfo: raise NotImplementedError
    def delete_secret(self, name: str) -> None: raise NotImplementedError

    # system
    def system_status(self) -> s.SystemStatus: raise NotImplementedError
    def selftest(self) -> s.SelfTestResult: raise NotImplementedError
    def update_info(self) -> s.UpdateInfo: raise NotImplementedError

    # logging the agent in to Claude (M5 wizard); online only
    def claude_login_start(self) -> s.ClaudeLoginStart: raise NotImplementedError
    def claude_login_code(self, req: s.ClaudeLoginCode) -> s.ClaudeLoginResult: raise NotImplementedError

    # notifications (M5)
    def push_key(self) -> s.PushKey: raise NotImplementedError
    def push_subscribe(self, req: s.PushSubscribe) -> s.NotificationSettings: raise NotImplementedError
    def push_unsubscribe(self, sub_id: int) -> None: raise NotImplementedError
    def notification_settings(self) -> s.NotificationSettings: raise NotImplementedError
    def set_notification_settings(self, req: s.NotificationSettings) -> s.NotificationSettings: raise NotImplementedError
    def push_test(self) -> s.PushTestResult: raise NotImplementedError
    def check_update(self) -> s.UpdateInfo: raise NotImplementedError
    def cancel_update(self) -> s.UpdateInfo: raise NotImplementedError

    # files
    def list_dir(self, volume: str, path: str) -> s.Listing: raise NotImplementedError
    def read_file(self, volume: str, path: str) -> tuple[bytes, str]: raise NotImplementedError
    def stream_file(self, volume: str, path: str) -> tuple[Iterator[bytes], int, str]: raise NotImplementedError
    def file_history(self, volume: str, path: str) -> list[s.FileHistoryEntry]: raise NotImplementedError
    def file_diff(self, volume: str, path: str, rev: str) -> s.FileDiff: raise NotImplementedError
    def zip_dir(self, volume: str, path: str): raise NotImplementedError
    def preview_url(self, volume: str, path: str, files_origin: str) -> s.PreviewUrl: raise NotImplementedError
    def shelf_raw(self, item_id: int, version: int | None) -> tuple[bytes, str, str]: raise NotImplementedError
    def shelf_preview_url(self, item_id: int, version: int | None, files_origin: str) -> s.PreviewUrl: raise NotImplementedError
    def web_page(self, url: str) -> s.WebPage: raise NotImplementedError
    def shelf(self, project: str) -> list[s.ShelfItem]: raise NotImplementedError
    def add_to_shelf(self, project: str, req: s.ShelfCreate, by: str) -> s.ShelfItem: raise NotImplementedError
    def remove_from_shelf(self, item_id: int, by: str = "owner") -> None: raise NotImplementedError
    def shelf_versions(self, item_id: int) -> list[s.ShelfVersion]: raise NotImplementedError
    def upload(self, project: str | None, name: str, data: bytes, mime: str | None, idempotency_key: str | None) -> s.Upload: raise NotImplementedError
