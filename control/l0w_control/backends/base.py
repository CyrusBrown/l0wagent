"""The interface routes call. One implementation per backend:

- `mock.MockBackend`: in-memory fixtures for UI work (M0)
- the SQLite backend (M1 onwards)

Methods not implemented by a backend raise NotImplementedError, which the
app turns into 501, so the contract can run ahead of the implementation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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


class Unauthorized(Exception):
    pass


class LoginFailed(Exception):
    def __init__(self, problem: s.LoginError, status: int = 401):
        super().__init__(problem.error)
        self.problem = problem
        self.status = status


class Backend:
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

    # files
    def list_dir(self, volume: str, path: str) -> s.Listing: raise NotImplementedError
    def read_file(self, volume: str, path: str) -> tuple[bytes, str]: raise NotImplementedError
    def file_history(self, volume: str, path: str) -> list[s.FileHistoryEntry]: raise NotImplementedError
    def shelf(self, project: str) -> list[s.ShelfItem]: raise NotImplementedError
    def add_to_shelf(self, project: str, req: s.ShelfCreate, by: str) -> s.ShelfItem: raise NotImplementedError
    def remove_from_shelf(self, item_id: int) -> None: raise NotImplementedError
    def shelf_versions(self, item_id: int) -> list[s.ShelfVersion]: raise NotImplementedError
    def upload(self, project: str | None, name: str, data: bytes, mime: str | None, idempotency_key: str | None) -> s.Upload: raise NotImplementedError
