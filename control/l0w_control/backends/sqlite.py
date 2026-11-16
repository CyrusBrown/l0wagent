"""The real backend: SQLite, conversations, the run manager, the usage gate, the vault.

M1: accounts, meta/status/sync/events, runs and timelines, jobs, the system page and
self-test. M2: projects, threads, messages, asks, claims, suggestions, the inbox, Reply
now, search, uploads into _inbox/ and write-only secrets. M3: the file browser (symlink-safe,
git history via dulwich), the shelf and its watcher, and the files origin for previews.
M4: providers, budgets and the usage ledger, the wakeup plan and its scheduler (fallback,
sweep, routines), roles, the reviewer, promote requests and the job registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from .. import __version__, network
from ..api import schemas as s
from ..auth import Auth, iso, parse, utcnow
from ..budgets import LEVELS, OPENCODE_GO, RUNTIME, SUBSCRIPTION, TIERS, Budgets
from ..chat import Chats
from ..config import Env, Settings
from ..connector import Connector
from ..conv import Conversations
from ..db import Database
from ..files import Storage, guess_mime
from ..files_origin import Signer
from ..files_origin import make_app as files_app
from ..integrations import Integrations
from ..jobs import Jobs
from ..live import Live
from ..notify import Notifier
from ..reviewer import Reviewer
from ..routines import Routines
from ..runs.clients import EgressClient, RunnerClient, RunnerError
from ..runs.manager import RunManager, run_model
from ..runs.usage import Usage
from ..scheduler import Scheduler, wakeup_model
from ..shelf import Shelf
from ..updates import Updates
from ..vault import Vault
from .base import Backend, BadRequest, Conflict, NotFound

T = TypeVar("T")
UPLOAD_MAX = 200 * 1024 * 1024
API_VERSION = "0"
SELFTEST_NOTE = ("The network probes run inside the agent's computer, so they catch a misconfigured setup "
                 "(common on Docker Desktop), not deliberate tampering. Enforcement lives in control and egress.")


class SqliteBackend(Backend):
    def __init__(self, env: Env | None = None, db_path: str | None = None, runner: RunnerClient | None = None, egress: EgressClient | None = None):
        self.env = env or Env()
        if db_path is None:
            self.env.data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(self.env.data_dir / "control.db")
        self.db = Database(db_path)
        self.settings = Settings(self.db)
        self.live = Live(self.db)
        self.auth = Auth(self.db, self.settings, on_new_session=self._new_session_alert, on_lockout=self._lockout_notice)
        self.usage = Usage(self.env, self.db, self.settings)
        self.runner = runner or RunnerClient(self.env)
        self.egress = egress or EgressClient(self.env)
        self.vault = Vault(self.db, self.env.data_dir)
        self.conv = Conversations(self.db, self.live, self.vault, self.settings)
        self.conv.add_egress_rule = lambda pattern, mode, note: self._await(self.egress.add_rule(pattern, mode, note))
        self.conv.delete_egress_rule = lambda rule_id: self._await(self.egress.delete_rule(rule_id))
        self.budgets = Budgets(self.db, self.settings, self.usage, self.vault.fernet)
        self.budgets.ensure_defaults()
        self.routines = Routines(self.db, self.budgets)
        self.routines.ensure_builtin()
        self.conv.routines = self.routines
        self.integrations = Integrations(self.db, self.vault, self.conv.file_permission)
        self.conv.integrations = self.integrations
        self.runs_mgr = RunManager(self.env, self.db, self.settings, self.live, self.runner, self.egress, self.usage, self.conv, self.vault, self.budgets)
        self.scheduler = Scheduler(self.db, self.settings, self.budgets, self.runs_mgr, self.live)
        self.routines.scheduler = self.scheduler
        self.scheduler.housekeeping.append(self._housekeeping)
        self.reviewer = Reviewer(self.db, self.settings, self.budgets, self.runner, self.live, self.vault.redact)
        self.reviewer.runs = self.runs_mgr
        self.runs_mgr.reviewer = self.reviewer
        self.runs_mgr.brief_extra = self._brief_extra
        self.runs_mgr.on_finish.append(lambda r: r["status"] == "failed" and self.notifier.notify(
            "run_failed", f"Run #{r['id']} failed", r.get("result") or "", f"/runs/{r['id']}", "run", str(r["id"])))
        self._last_jobs_check = 0.0
        self.inbox_dir = os.environ.get("L0W_INBOX_DIR", "/work/_inbox")
        self.storage = Storage({"workspace": Path(os.environ.get("L0W_WORKSPACE_DIR", "/workspace")), "work": Path(os.environ.get("L0W_WORK_DIR", "/work"))})
        self.shelf_ = Shelf(self.db, self.live, self.storage, self.env.data_dir / "shelf")
        self.jobs_ = Jobs(self.db, self.live, self.storage, self.vault.redact)
        self.updates = Updates(self.db)
        self.notifier = Notifier(self.db, self.env.data_dir)
        self.conv.notify = self.notifier.notify
        self.chats_ = Chats(self.db, self, self.live, self.budgets, self.vault)
        self.connector = Connector(self.db, self)
        self.signer = Signer(self.vault.key)
        self.files_app = files_app(self.storage, self.signer, self.shelf_.raw)
        self._files_server = None
        self._seed_news()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.live.getters.update(run=lambda i: self.runs_mgr.run(int(i)), security_alert=lambda i: self._alert(int(i)))

    def _seed_news(self) -> None:
        from ..prompts.news import NEWS

        for date, title, body in NEWS:
            if not self.db.q1("SELECT 1 FROM harness_news WHERE date=? AND title=?", (date, title)):
                self.db.ex("INSERT INTO harness_news(date, title, body) VALUES(?,?,?)", (date, title, body))

    # ── lifecycle and bridging ─────────────────────────────────────────────
    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.notifier.loop = self.loop
        self.live.bind(self.loop)
        self.chats_.startup()
        await self.runs_mgr.startup()
        self._watcher = asyncio.create_task(self.shelf_.watch(self.runs_mgr.active_run_id))
        self._scheduler = asyncio.create_task(self.scheduler.loop()) if os.environ.get("L0W_SCHEDULER", "1") != "0" else None
        self._reviewer = asyncio.create_task(self.reviewer.loop())
        port = os.environ.get("L0W_FILES_PORT")
        if port:  # the files origin: a second port served by this process
            import uvicorn

            self._files_server = uvicorn.Server(uvicorn.Config(self.files_app, host=os.environ.get("L0W_FILES_HOST", "0.0.0.0"), port=int(port),
                                                               lifespan="off", log_level="warning"))
            self._files_task = asyncio.create_task(self._files_server.serve())

    async def shutdown(self) -> None:
        self._watcher.cancel()
        if self._scheduler:
            self._scheduler.cancel()
        self._reviewer.cancel()
        if self._files_server:
            self._files_server.should_exit = True
            await asyncio.gather(self._files_task, return_exceptions=True)
        await self.runs_mgr.shutdown()

    def _await(self, coro: Coroutine[Any, Any, T], timeout: float = 60) -> T:
        """Run a coroutine on the app's loop from a request thread."""
        if self.loop is None:
            coro.close()
            raise RuntimeError("backend not started")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ── meta, status, sync, events ─────────────────────────────────────────
    def meta(self) -> s.Meta:
        return s.Meta(server_version=__version__, api_version=API_VERSION, ui_bundle_version=__version__, capabilities=s.Capabilities(),
                      setup_complete=self.auth.owner() is not None, instance_name=self.env.instance_name)

    def status(self) -> s.Status:
        run_id = self.runs_mgr.active_run_id()
        run = self.runs_mgr.run(run_id) if run_id else None
        pid = self.budgets.role("worker").provider
        nxt = self.scheduler.next_wakeup()
        state = "working" if run else "asleep" if nxt else "dormant"
        return s.Status(
            state=state, now=run.now if run else None, run_id=run_id, run_used=run.used if run else None,  # type: ignore[arg-type]
            run_budget=run.budget if run else None, next_wakeup_at=nxt.at if nxt else None, budget=self.budgets.budget_summary(pid), usage=self.usage.cached(),
            inbox_waiting=self.inbox_counts().waiting, reply_budget_left=self.budgets.reply_left(self.budgets.role("replier").provider),
        )

    def sync(self, since: str | None) -> s.SyncResponse:
        def snapshot() -> list[tuple[str, str]]:
            out = [("run", str(r["id"])) for r in self.db.q("SELECT id FROM runs ORDER BY id DESC LIMIT 200")]
            out += [("security_alert", str(r["id"])) for r in self.db.q("SELECT id FROM security_alerts WHERE status='open'")]
            out += [("project", r["slug"]) for r in self.db.q("SELECT slug FROM projects")]
            out += [("thread", str(r["id"])) for r in self.db.q("SELECT id FROM threads WHERE status='open'")]
            out += [("message", str(r["id"])) for r in self.db.q(
                "SELECT m.id FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.status='open' ORDER BY m.id DESC LIMIT 2000")]
            for entity, table in (("question", "questions"), ("permission", "permissions"), ("task", "tasks"), ("suggestion", "suggestions"), ("promote", "promote_requests")):
                out += [(entity, str(r["id"])) for r in self.db.q(f"SELECT id FROM {table} WHERE status='open'")]
            out += [("claim", str(r["id"])) for r in self.db.q("SELECT id FROM claims WHERE status IN ('planned','in_progress')")]
            return out

        return self.live.sync(since, snapshot)

    def events(self) -> AsyncIterator[s.LiveEvent]:
        return self.live.stream()

    # ── auth ───────────────────────────────────────────────────────────────
    def authenticate(self, session_cookie, bearer):
        return self.auth.authenticate(session_cookie, bearer)

    def login(self, req, ip, user_agent):
        return self.auth.login(req, ip, user_agent)

    def login_totp(self, req, ip, user_agent):
        return self.auth.login_totp(req, ip, user_agent)

    def logout(self, session_cookie, bearer):
        self.auth.logout(session_cookie, bearer)

    def setup_owner(self, req):
        owner = self.auth.setup_owner(req)
        self.live.emit("status")
        return owner

    def auth_sessions(self, current_cookie, current_bearer):
        return self.auth.sessions(current_cookie, current_bearer)

    def revoke_session(self, session_id):
        self.auth.revoke(session_id)
        for a in self.db.q("SELECT id FROM security_alerts WHERE session_id=? AND status='open'", (session_id,)):
            self.db.ex("UPDATE security_alerts SET status='revoked', decided_at=? WHERE id=?", (self.db.now(), a["id"]))
            self.live.changed("security_alert", a["id"])
        self.live.emit("inbox_counts")

    def change_password(self, req, current_cookie, current_bearer):
        self.auth.change_password(req, current_cookie, current_bearer)

    def totp_setup(self):
        return self.auth.totp_setup()

    def totp_enable(self, req):
        return self.auth.totp_enable(req)

    def totp_disable(self, req):
        return self.auth.totp_disable(req)

    def _new_session_alert(self, session_id: str, label: str, ip: str | None) -> None:
        now = self.db.now()
        text = f"New sign-in: {label}" + (f" from {ip}" if ip else "")
        aid = self.db.ex("INSERT INTO security_alerts(session_id, text, created_at) VALUES(?,?,?)", (session_id, text, now))
        self.notifier.notify("new_device", text, "If this wasn't you, revoke it from your Inbox.", "/inbox", "security_alert", str(aid))
        self.live.changed("security_alert", aid)
        self.live.emit("inbox_counts")

    def _lockout_notice(self, ip: str, seconds: int) -> None:
        from datetime import timedelta

        from ..auth import iso

        recent = self.db.q1("SELECT id FROM notifications WHERE kind='lockout' AND created_at >= ?", (iso(utcnow() - timedelta(minutes=15)),))
        if not recent:
            self.db.ex("INSERT INTO notifications(kind, title, created_at) VALUES('lockout',?,?)",
                       (f"Sign-in locked for {seconds // 60 + 1} min after repeated failures (last from {ip})", self.db.now()))

    # ── inbox ──────────────────────────────────────────────────────────────
    def _alert(self, aid: int) -> s.SecurityAlert:
        r = self.db.q1("SELECT * FROM security_alerts WHERE id=?", (aid,))
        if not r:
            raise NotFound(f"security alert {aid}")
        return s.SecurityAlert(id=r["id"], session_id=r["session_id"], text=r["text"], status=r["status"], created_at=parse(r["created_at"]))  # type: ignore[arg-type]

    def inbox(self, view: str, kind: str | None, q: str | None) -> list[s.InboxEntry]:
        waiting = view == "waiting"
        where = "status='open'" if waiting else "status!='open'"
        alerts = [("security", self._alert(r["id"])) for r in self.db.q(f"SELECT id FROM security_alerts WHERE {where} ORDER BY id DESC LIMIT 200")]
        entries = self.conv.inbox_entries(waiting, alerts)
        if kind:
            entries = [e for e in entries if e.kind == kind]
        if q:
            ql = q.lower()
            entries = [e for e in entries if ql in e.model_dump_json().lower()]
        return entries if waiting else entries[:300]

    def inbox_counts(self) -> s.InboxCounts:
        by = self.conv.counts()
        return s.InboxCounts(waiting=sum(by.values()), by_kind=by)  # type: ignore[arg-type]

    def decide_security(self, aid: int, req: s.SecurityDecision) -> s.SecurityAlert:
        a = self._alert(aid)
        if a.status != "open":
            raise Conflict("already_decided", f"security alert {aid} is {a.status}")
        if req.decision == "revoke":
            try:
                self.auth.revoke(a.session_id)
            except NotFound:
                pass
        self.db.ex("UPDATE security_alerts SET status=?, decided_at=? WHERE id=?", ("confirmed" if req.decision == "confirm" else "revoked", self.db.now(), aid))
        self.db.ex("UPDATE auth_sessions SET confirmed_at=? WHERE id=? AND ?='confirm'", (self.db.now(), a.session_id, req.decision))
        self.live.changed("security_alert", aid)
        self.live.emit("inbox_counts")
        return self._alert(aid)

    def answer_question(self, qid, req):
        return self.conv.answer_question(qid, req)

    def decide_permission(self, pid, req):
        return self.conv.decide_permission(pid, req)

    def resolve_task(self, tid, req):
        return self.conv.resolve_task(tid, req)

    def decide_suggestion(self, sid, req):
        return self.conv.decide_suggestion(sid, req)

    def accept_all_suggestions(self):
        return self.conv.accept_all()

    def decide_promote(self, pid, req):
        pr = self.conv.decide_promote(pid, req)
        if pr.status == "approved" and not self.runs_mgr.promote(pr.run_id, pr.budget):
            self.db.ex("UPDATE promote_requests SET note=COALESCE(note || ' ', '') || ? WHERE id=?", ("(the run had already ended)", pid))
        return self.conv.promote(pid)

    def claims(self, active: bool) -> list[s.Claim]:
        return self.conv.claims(active)

    # ── projects, threads, messages ────────────────────────────────────────
    def projects(self, status):
        return self.conv.projects(status)

    def project(self, slug):
        return self.conv.project(slug)

    def create_project(self, req: s.ProjectCreate) -> s.Project:
        return self.conv.once(req.idempotency_key, "project", s.Project, lambda: self.conv.create_project(req.name, req.slug, req.summary, "owner"))

    def patch_project(self, slug, req: s.ProjectPatch):
        return self.conv.patch_project(slug, req.model_dump(exclude_none=True))

    def threads(self, project, general, archived):
        return self.conv.threads(project, general, archived)

    def thread(self, thread_id):
        return self.conv.thread(thread_id)

    def create_thread(self, req: s.ThreadCreate) -> s.Thread:
        return self.conv.once(req.idempotency_key, "thread", s.Thread, lambda: self.conv.create_thread(req.title, req.project, "owner", req.first_message, upload_ids=req.upload_ids))

    def patch_thread(self, thread_id, req: s.ThreadPatch):
        return self.conv.patch_thread(thread_id, req.model_dump(exclude_unset=True))

    def thread_items(self, thread_id, before, limit):
        return self.conv.thread_items(thread_id, before, limit)

    def post_message(self, thread_id, req: s.MessageCreate):
        return self.conv.post_owner_message(thread_id, req.body, req.upload_ids, req.idempotency_key)

    def recent_messages(self, sender, limit):
        return self.conv.recent_messages(sender, limit)

    def mark_read(self, thread_id, message_id):
        self.conv.mark_read(thread_id, message_id)

    def delivery(self, thread_id: int) -> s.Delivery:
        self.conv.thread(thread_id)
        run_id = self.runs_mgr.active_run_id()
        if run_id:
            run = self.db.q1("SELECT waiting_until FROM runs WHERE id=?", (run_id,))
            text = "Waiting for your reply · sees it right away" if run and run["waiting_until"] else "Working now · sees it after its next step"
            return s.Delivery(state="working", text=text, next_wakeup_at=None, reply_now_available=False, reply_now_cost=None, reply_budget_left=None)
        replier = self.budgets.role("replier").provider
        left = self.budgets.reply_left(replier)
        gate_open = self.usage.gate(self.usage.cached()).open if replier == SUBSCRIPTION else bool(self.budgets.credential(replier))
        nxt = self.scheduler.next_wakeup()
        if nxt:
            text = f"Queued for the next wakeup ({self.budgets.local(nxt.at):%a %H:%M})"
        elif self.budgets.effective(self.budgets.role("worker").provider).limits.allowance <= 0:
            return s.Delivery(state="dormant", text="Dormant: no wakeups planned. Reply now still works." if left > 0 else "Dormant, and the reply budget is used up",
                              next_wakeup_at=None, reply_now_available=gate_open and left > 0, reply_now_cost=min(self.settings.get("REPLY_RUN_MAX_PTS"), left) if left > 0 else None,
                              reply_budget_left=round(left, 1))
        else:
            text = "Queued for the assistant's next run"
        return s.Delivery(state="queued", text=text, next_wakeup_at=nxt.at if nxt else None, reply_now_available=gate_open and left > 0,
                          reply_now_cost=min(self.settings.get("REPLY_RUN_MAX_PTS"), left) if left > 0 else None, reply_budget_left=round(left, 1))

    def reply_now(self, thread_id: int) -> s.Run:
        t = self.conv.thread(thread_id)
        if self.runs_mgr.active_run_id() is not None:
            raise Conflict("already_running", "The assistant is working now and will see your message after its next step.")
        return self._await(self.runs_mgr.start(s.RunStart(reason=f"“{t.title}”"), kind="reply", why="owner_message", thread_id=thread_id))

    def search(self, q, kind, project, since):
        return self.conv.search(q, kind, project, since)

    # ── uploads (control's only write into the agent's storage) ────────────
    def upload(self, project: str | None, name: str, data: bytes, mime: str | None, idempotency_key: str | None) -> s.Upload:
        def make() -> s.Upload:
            if len(data) > UPLOAD_MAX:
                raise BadRequest("too_large", f"Uploads are limited to {UPLOAD_MAX // 1024 // 1024} MB.")
            pid = self.conv._project_row(project)["id"] if project else None
            sub = project or "general"
            base = os.path.basename(name.replace("\\", "/")).strip().lstrip(".") or "upload"
            base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')[:120] or "upload"
            stored = self._write_inbox(sub, base, data)
            uid = self.db.ex("INSERT INTO uploads(project_id, name, path, size, mime, sha256, created_at, client_id) VALUES(?,?,?,?,?,?,?,?)",
                             (pid, base, f"_inbox/{sub}/{stored}", len(data), mime or mimetypes.guess_type(base)[0], hashlib.sha256(data).hexdigest(),
                              self.db.now(), idempotency_key))
            self.live.emit("upload", {"id": uid})
            return self._upload(uid)

        return self.conv.once(idempotency_key, "upload", s.Upload, make)

    def _upload(self, uid: int) -> s.Upload:
        u = self.db.q1("SELECT u.*, p.slug FROM uploads u LEFT JOIN projects p ON p.id=u.project_id WHERE u.id=?", (uid,))
        assert u
        return s.Upload(id=u["id"], project=u["slug"], name=u["name"], path=u["path"], size=u["size"], mime=u["mime"], created_at=parse(u["created_at"]))  # type: ignore[arg-type]

    def _write_inbox(self, sub: str, name: str, data: bytes) -> str:
        """Write into <inbox>/<sub>/ without following symlinks below the inbox root (the
        agent controls that tree). Never overwrites: picks a free name."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            root = os.open(self.inbox_dir, flags)
        except OSError as e:
            raise Conflict("inbox_unavailable", f"Control can't write uploads here ({self.inbox_dir}): {e}") from e
        try:
            try:
                os.mkdir(sub, 0o755, dir_fd=root)
            except FileExistsError:
                pass
            try:
                d = os.open(sub, flags | nofollow, dir_fd=root)
            except OSError as e:
                raise Conflict("inbox_unavailable", f"_inbox/{sub} isn't a plain folder") from e
            try:
                stem, dot, ext = name.rpartition(".")
                if not dot or not stem:
                    stem, dot, ext = name, "", ""
                for n in range(1000):
                    candidate = name if n == 0 else f"{stem}-{n + 1}{dot}{ext}"
                    try:
                        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644, dir_fd=d)
                    except FileExistsError:
                        continue
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    return candidate
                raise Conflict("name_taken", "Too many files with that name.")
            finally:
                os.close(d)
        finally:
            os.close(root)

    # ── files ──────────────────────────────────────────────────────────────
    def _run_at(self, t: float) -> int | None:
        """The run that was going at a time (for commits the agent made itself)."""
        from datetime import UTC, datetime

        from ..auth import iso

        at = iso(datetime.fromtimestamp(t, UTC))
        row = self.db.q1("SELECT id FROM runs WHERE started_at<=? AND (ended_at IS NULL OR ended_at>=?) ORDER BY id DESC LIMIT 1", (at, at))
        return row["id"] if row else None

    def list_dir(self, volume: str, path: str) -> s.Listing:
        from datetime import UTC, datetime

        path = "/".join(p for p in path.split("/") if p)
        entries = self.storage.list(volume, path)
        changes = self.storage.last_changes(volume, path)
        repo = self.storage.repo_for(volume, path)
        branch = None
        if repo:
            branch = self.storage.branch(repo[0])
            repo[0].close()
        inbox = volume == "work" and (path == "_inbox" or path.startswith("_inbox/"))
        out = []
        for e in entries:
            run = None
            if not e.dir and e.name in changes:
                run, t = changes[e.name]
                run = run or self._run_at(t)
            out.append(s.FileEntry(name=e.name, dir=e.dir, size=e.size, modified_at=datetime.fromtimestamp(e.mtime, UTC) if e.mtime else None,
                                   last_run_id=run, from_owner=inbox or (volume == "work" and not path and e.name == "_inbox"), symlink=e.symlink))
        return s.Listing(volume=volume, path=path, git=repo is not None, branch=branch, entries=out)  # type: ignore[arg-type]

    def read_file(self, volume: str, path: str) -> tuple[bytes, str]:
        return self.storage.read(volume, path, 200 * 1024 * 1024), guess_mime(path)

    def file_history(self, volume: str, path: str) -> list[s.FileHistoryEntry]:
        return [s.FileHistoryEntry(rev=h["rev"], run_id=h["run_id"] or self._run_at(h["time"]), at=h["at"], added=h["added"], removed=h["removed"], message=h["message"])
                for h in self.storage.history(volume, path)]

    def file_diff(self, volume: str, path: str, rev: str) -> s.FileDiff:
        d = self.storage.diff(volume, path, rev)
        return s.FileDiff(rev=d["rev"], parent=d["parent"], at=d["at"], message=d["message"], run_id=d["run_id"] or self._run_at(d["time"]),
                          added=d["added"], removed=d["removed"], patch=d["patch"])

    def zip_dir(self, volume: str, path: str):
        return self.storage.zip(volume, path)

    def _signed(self, scope: dict, name: str, origin: str) -> s.PreviewUrl:
        from datetime import UTC, datetime, timedelta
        from urllib.parse import quote

        from ..files_origin import TTL_S

        token = self.signer.sign(scope)
        return s.PreviewUrl(url=f"{origin}/f/{token}/{quote(name)}", expires_at=datetime.now(UTC) + timedelta(seconds=TTL_S))

    def preview_url(self, volume: str, path: str, files_origin: str) -> s.PreviewUrl:
        self.storage.stat(volume, path)
        folder, _, name = path.rpartition("/")
        if guess_mime(name) in ("text/html", "application/xhtml+xml"):
            return self._signed({"k": "dir", "v": volume, "p": folder}, name, files_origin)
        return self._signed({"k": "file", "v": volume, "p": path}, name, files_origin)

    def shelf(self, project: str) -> list[s.ShelfItem]:
        return self.shelf_.items(self.conv._project_row(project)["id"])

    def add_to_shelf(self, project: str, req: s.ShelfCreate, by: str, run_id: int | None = None) -> s.ShelfItem:
        pid = self.conv._project_row(project)["id"]
        path = "/".join(p for p in req.path.split("/") if p)
        item = self.conv.once(req.idempotency_key, "shelf", s.ShelfItem,
                              lambda: self.shelf_.add(pid, req.volume, path, req.label, req.mode, req.keep_versions, by, run_id))
        self.live.changed("project", project)
        return item

    def remove_from_shelf(self, item_id: int, by: str = "owner") -> None:
        self.shelf_.remove(item_id, by)

    def shelf_versions(self, item_id: int) -> list[s.ShelfVersion]:
        return self.shelf_.versions(item_id)

    def shelf_raw(self, item_id: int, version: int | None) -> tuple[bytes, str, str]:
        return self.shelf_.raw(item_id, version)

    def shelf_preview_url(self, item_id: int, version: int | None, files_origin: str) -> s.PreviewUrl:
        it = self.shelf_.item(item_id)
        if it.mode == "live" and version is None and guess_mime(it.path) in ("text/html", "application/xhtml+xml"):
            folder, _, name = it.path.rpartition("/")
            return self._signed({"k": "dir", "v": it.volume, "p": folder}, name, files_origin)
        return self._signed({"k": "shelf", "i": item_id, "n": version}, it.path.rsplit("/", 1)[-1], files_origin)

    def web_page(self, url: str) -> s.WebPage:
        import socket

        host, _, port = os.environ.get("L0W_AGENT_WEB_TARGET", "agent:8787").rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=1):
                reachable = True
        except OSError:
            reachable = False
        return s.WebPage(url=url, reachable=reachable)

    # ── network ────────────────────────────────────────────────────────────
    def network(self, filter: str = "all") -> s.NetworkView:
        async def load() -> tuple:
            day = iso(utcnow() - timedelta(days=1)).replace("Z", "+00:00")  # egress's format
            params: dict = {"limit": 200, **network.FILTERS.get(filter, {})}
            if filter == "hour":
                params["since"] = iso(utcnow() - timedelta(hours=1)).replace("Z", "+00:00")
            st, rules, reqs = await self.egress.status(), await self.egress.rules(), await self.egress.requests(**params)
            day_all = await self.egress.requests(limit=1, since=day)
            day_blocked = await self.egress.requests(limit=1, since=day, action="blocked")
            return st, rules, reqs, day_all["total"], day_blocked["total"], await self.egress.host_stats(1), await self.egress.host_stats(7)
        try:
            st, rules, reqs, n24, b24, hosts1, hosts7 = self._await(load(), timeout=15)
        except Exception:  # noqa: BLE001
            return s.NetworkView(reachable=False, default_policy="allow", rules=[], requests=[], requests_24h=0, blocked_24h=0, top_hosts=[])
        return network.view(st, rules, reqs["requests"], n24, b24, hosts1, hosts7)

    def add_egress_rule(self, req: s.EgressRuleCreate) -> s.EgressRule:
        pattern = network.check_pattern(req.pattern)

        def make() -> s.EgressRule:
            try:
                rid = self._await(self.egress.add_rule(pattern, req.action, self.vault.redact(req.note.strip()) or ""), timeout=15)
            except RunnerError as e:
                raise BadRequest("egress_refused", str(e).removeprefix("egress: ")) from None
            return next(r for r in self.network().rules if r.id == rid)
        return self.conv.once(req.idempotency_key, "egress_rule", s.EgressRule, make)

    # ── the MCP connector ──────────────────────────────────────────────────
    def connect_request(self, params: dict[str, str]) -> s.ConnectRequest:
        from ..connector import OAuthError
        try:
            return s.ConnectRequest(**self.connector.describe(params))
        except OAuthError as e:
            raise BadRequest(e.error, e.description) from None

    def connect_consent(self, req: s.ConnectConsent) -> s.ConnectRedirect:
        from ..connector import OAuthError
        try:
            return s.ConnectRedirect(redirect=self.connector.consent(req.params, req.approve, req.scopes))
        except OAuthError as e:
            raise BadRequest(e.error, e.description) from None

    def connected_apps(self) -> list[s.ConnectedApp]:
        return [s.ConnectedApp(**{**a, "created_at": parse(a["created_at"]), "last_used_at": parse(a["last_used_at"])}) for a in self.connector.apps()]

    def revoke_connected_app(self, app_id: int) -> None:
        self.connector.revoke_app(app_id)

    def delete_egress_rule(self, rule_id: int) -> None:
        self._await(self.egress.delete_rule(rule_id), timeout=15)

    # ── integrations ───────────────────────────────────────────────────────
    @staticmethod
    def _integration(d: dict) -> s.Integration:
        return s.Integration(**{**d, "synced_at": parse(d["synced_at"]), "created_at": parse(d["created_at"]),
                                "tools": [s.IntegrationTool(**t) for t in d["tools"]],
                                "recent_calls": [s.IntegrationCall(**{**c, "at": parse(c["at"])}) for c in d["recent_calls"]]})

    def list_integrations(self) -> list[s.Integration]:
        return [self._integration(d) for d in self.integrations.listing()]

    def integration_presets(self) -> list[s.IntegrationPreset]:
        return [s.IntegrationPreset(**p) for p in self.integrations.presets()]

    def create_integration(self, req: s.IntegrationCreate) -> s.Integration:
        def make() -> s.Integration:
            return self._integration(self.integrations.create(req.name or "", req.title, req.preset, req.url, req.auth_secret))
        return self.conv.once(req.idempotency_key, "integration", s.Integration, make)

    def get_integration(self, name: str) -> s.Integration:
        return self._integration(self.integrations.get(name))

    def update_integration(self, name: str, req: s.IntegrationPatch) -> s.Integration:
        return self._integration(self.integrations.update(name, **req.model_dump(exclude_unset=True)))

    def delete_integration(self, name: str) -> None:
        self.integrations.delete(name)

    def sync_integration(self, name: str) -> s.Integration:
        return self._integration(self.integrations.sync(name))

    def set_integration_tool_policy(self, name: str, tool: str, policy: str) -> s.Integration:
        return self._integration(self.integrations.set_policy(name, tool, policy))

    # ── secrets ────────────────────────────────────────────────────────────
    def _secret(self, name: str) -> s.SecretInfo:
        r = next((x for x in self.vault.listing() if x["name"] == name.upper()), None)
        if not r:
            raise NotFound(f"secret {name}")
        return s.SecretInfo(name=r["name"], description=r["description"], scope=r["scope"], scope_label=self.vault.scope_label(r["scope"]), created_at=parse(r["created_at"]),  # type: ignore[arg-type]
                            updated_at=parse(r["updated_at"]), reads=r["reads"], last_read_at=parse(r["last_read_at"]))  # type: ignore[arg-type]

    def secrets(self) -> list[s.SecretInfo]:
        return [self._secret(r["name"]) for r in self.vault.listing()]

    # ── 1:1 chats ──────────────────────────────────────────────────────────
    def chats(self, archived: bool) -> list[s.Chat]:
        return self.chats_.chats(archived)

    def chat(self, chat_id: int) -> s.Chat:
        return self.chats_.chat(chat_id)

    def create_chat(self, req: s.ChatCreate) -> s.Chat:
        return self.chats_.create(req)

    def patch_chat(self, chat_id: int, req: s.ChatPatch) -> s.Chat:
        return self.chats_.patch(chat_id, req)

    def chat_messages(self, chat_id: int) -> list[s.ChatMessage]:
        return self.chats_.messages(chat_id)

    def send_chat_message(self, chat_id: int, req: s.ChatMessageCreate) -> s.ChatMessage:
        return self.chats_.send(chat_id, req)

    def stop_chat(self, chat_id: int) -> s.Chat:
        return self.chats_.stop(chat_id)

    def decide_chat_card(self, chat_id: int, message_id: int, req: s.ChatCardDecision) -> s.ChatMessage:
        return self.chats_.decide(chat_id, message_id, req)

    def chat_handoff(self, chat_id: int, req: s.ChatHandoff) -> s.Chat:
        return self.chats_.handoff(chat_id, req)

    def chat_ask(self, chat_id: int, req: s.ChatAsk) -> s.ChatMessage:
        return self.chats_.ask(chat_id, req)

    def chat_pull(self, chat_id: int, req: s.ChatPull) -> s.Chat:
        return self.chats_.pull(chat_id, req.thread_id)

    def upload_chat_file(self, name: str, data: bytes, mime: str | None) -> s.ChatFile:
        return self.chats_.upload(name, data, mime)

    def chat_file(self, file_id: int) -> tuple[bytes, str, str]:
        return self.chats_.file(file_id)

    def set_secret(self, name: str, req: s.SecretSet) -> s.SecretInfo:
        out = self.vault.set(name, req.value, req.description, req.scope)
        self.live.emit("status")
        return self._secret(out["name"])

    def delete_secret(self, name: str) -> None:
        self.vault.delete(name)

    # ── runs ───────────────────────────────────────────────────────────────
    def runs(self, kind: str | None, project: str | None, limit: int) -> list[s.Run]:
        rows = self.db.q("SELECT * FROM runs WHERE (? IS NULL OR kind=?) ORDER BY id DESC LIMIT ?", (kind, kind, limit if not project else 2000))
        runs = [run_model(self.db, r) for r in rows]
        return [r for r in runs if not project or project in r.projects][:limit]

    def run(self, run_id: int) -> s.RunDetail:
        run = self.runs_mgr.run(run_id)
        claims = self.conv.claims(active=False, run_id=run_id)
        asks = [f"Question: {r['text']}" for r in self.db.q("SELECT text FROM questions WHERE run_id=?", (run_id,))]
        asks += [f"Permission: {r['text']}" for r in self.db.q("SELECT text FROM permissions WHERE run_id=?", (run_id,))]
        asks += [f"Task: {r['text']}" for r in self.db.q("SELECT text FROM tasks WHERE run_id=?", (run_id,))]
        threads = [f"Messaged “{r['title']}”" for r in self.db.q("SELECT DISTINCT t.title FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.run_id=?", (run_id,))]
        threads += [f"Suggested: {r['text']}" for r in self.db.q("SELECT text FROM suggestions WHERE run_id=?", (run_id,))]
        memory: list[s.FileChange] = []
        for item in self.db.q("SELECT data_json FROM timeline_items WHERE run_id=? AND kind='diff'", (run_id,)):
            data = json.loads(item["data_json"])
            if data.get("volume") == "workspace":
                memory += [s.FileChange(path=f["path"], added=f.get("added"), removed=f.get("removed")) for f in data.get("files", [])]
        work = _work_changes(self.db.q("SELECT data_json FROM timeline_items WHERE run_id=? AND kind='tool' ORDER BY id", (run_id,)))
        shelved = [f"{r['label']} ({r['mode']})" for r in self.db.q("SELECT label, mode FROM shelf_items WHERE run_id=? ORDER BY id", (run_id,))]
        asks += [f"Promote: {r['reason']} ({r['status']})" for r in self.db.q("SELECT reason, status FROM promote_requests WHERE run_id=?", (run_id,))]
        threads += [f"Planned a wakeup: {r['reason']}" for r in self.db.q("SELECT reason FROM wakeups WHERE created_by='assistant' AND created_at>=? AND created_at<=? ORDER BY id",
                                                                           (iso(run.started_at), iso(run.ended_at or utcnow())))]
        return s.RunDetail(run=run, claims=claims, changes=s.RunChanges(memory=memory, work=work, shelved=shelved, asks=asks, threads=threads),
                           reviewer_mode=run.reviewer_mode, level=run.level)

    def timeline(self, run_id: int, view: str, kinds: list[str] | None, after: int | None) -> list[s.TimelineItem]:
        self.runs_mgr.run(run_id)
        sql, params = "SELECT * FROM timeline_items WHERE run_id=?", [run_id]
        if view == "simple":
            sql += " AND simple=1"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        if after is not None:
            sql += " AND id>?"
            params.append(after)
        rows = self.db.q(sql + " ORDER BY id LIMIT 5000", params)
        return [s.TimelineItem(id=r["id"], run_id=run_id, at=parse(r["at"]), kind=r["kind"], simple=bool(r["simple"]), text=r["text"], data=json.loads(r["data_json"]))  # type: ignore[arg-type]
                for r in rows]

    def start_run(self, req: s.RunStart) -> s.Run:
        return self._await(self.runs_mgr.start(req))

    def stop_run(self, run_id: int) -> s.Run:
        return self._await(self.runs_mgr.stop(run_id))

    def jobs(self, include_done: bool) -> list[s.Job]:
        return self.jobs_.list(include_done)

    def _brief_extra(self, run: dict) -> str:
        parts = []
        jobs = self.jobs_.brief_text()
        if jobs:
            parts.append("## Long jobs still running\n\n" + jobs)
        routines = self.routines.occurrences_text(self.budgets.now())
        if routines:
            parts.append("## Routines\n\n" + routines)
        projects = [p for p in self.conv.projects(None) if p.status != "archived"]
        if projects:
            parts.append("## Projects\n\n" + "\n".join(f"- {p.slug}: {p.name} ({p.status})" + (f" · {p.summary}" if p.summary else "") for p in projects))
        return "\n\n".join(parts)

    async def _housekeeping(self, now) -> None:
        from datetime import timedelta
        if self.settings.get("UPDATE_CHECK"):
            before = self.updates.info().available
            await self.updates.check()
            if self.updates.info().available and not before:
                self.live.emit("status")
        self.conv.expire_promotes(older_than=iso(now - timedelta(minutes=self.settings.get("PROMOTE_WAIT_MINUTES"))))
        pids = self.jobs_.running_pids()
        if pids and now.timestamp() - self._last_jobs_check >= 60:
            self._last_jobs_check = now.timestamp()
            try:
                alive = await self.runner.procs(pids)
            except RunnerError:
                return
            self.jobs_.mark_lost(alive)

    # ── logging the agent in to Claude ─────────────────────────────────────
    def claude_login_start(self) -> s.ClaudeLoginStart:
        if self.runs_mgr.active_run_id() is not None:
            raise Conflict("busy", "A run is in progress; log in once it's done.")

        async def start() -> dict:
            await self.egress.acquire("claude-login", 900, "logging the agent in to Claude")
            try:
                return await self.runner.login_start()
            except RunnerError:
                await self.egress.release("claude-login", "login failed to start")
                raise

        try:
            r = self._await(start())
        except RunnerError as e:
            raise Conflict("login_failed", f"The assistant's computer couldn't start the login: {e}") from e
        return s.ClaudeLoginStart(url=r["url"])

    def claude_login_code(self, req: s.ClaudeLoginCode) -> s.ClaudeLoginResult:
        async def finish() -> dict:
            try:
                return await self.runner.login_code(req.code)
            finally:
                await self.egress.release("claude-login", "login finished")

        try:
            r = self._await(finish(), timeout=150)
        except RunnerError as e:
            return s.ClaudeLoginResult(ok=False, message=f"The login didn't finish: {e}")
        self.usage._cache = None
        if r.get("ok"):
            self._await(self.usage.read(max_age=0))
            self.live.emit("status")
            return s.ClaudeLoginResult(ok=True, message="The assistant is logged in to Claude.")
        return s.ClaudeLoginResult(ok=False, message="The code didn't work. Start again and paste the newest code. " + (r.get("output") or "")[-300:])

    # ── notifications ──────────────────────────────────────────────────────
    def push_key(self) -> s.PushKey:
        return s.PushKey(public_key=self.notifier.public_key())

    def push_subscribe(self, req: s.PushSubscribe) -> s.NotificationSettings:
        self.notifier.subscribe(req)
        return self.notifier.settings()

    def push_unsubscribe(self, sub_id: int) -> None:
        self.notifier.unsubscribe(sub_id=sub_id)

    def notification_settings(self) -> s.NotificationSettings:
        return self.notifier.settings()

    def set_notification_settings(self, req: s.NotificationSettings) -> s.NotificationSettings:
        return self.notifier.save_settings(req)

    def push_test(self) -> s.PushTestResult:
        return s.PushTestResult(sent=self._await(self.notifier.test()))

    # ── updates ────────────────────────────────────────────────────────────
    def update_info(self) -> s.UpdateInfo:
        return self.updates.info()

    def check_update(self) -> s.UpdateInfo:
        self._await(self.updates.check(force=True))
        return self.updates.info()

    # ── budgets and providers ──────────────────────────────────────────────
    def budget_overview(self) -> s.BudgetOverview:
        return s.BudgetOverview(
            providers=[self.budgets.summary(p) for p in sorted(self.budgets.provider_ids(), key=lambda p: p != self.budgets.role("worker").provider)],  # the worker's first
            levels=[s.LevelPreset(name=n, text=t, limits=s.BudgetLimits(**vars(lim))) for n, (t, lim) in LEVELS.items()],  # type: ignore[arg-type]
            tiers=list(TIERS), reserve_weekly_pct=self.settings.get("RESERVE_WEEKLY_PCT"), reserve_five_hour_pct=self.settings.get("RESERVE_FIVE_HOUR_PCT"),
            sweep_window_hours=self.settings.get("SWEEP_WINDOW_HOURS"), sweep_ceiling_pct=self.settings.get("SWEEP_CEILING_PCT"),
        )

    def _budget_changed(self) -> None:
        self.live.emit("budget")
        self.live.emit("status")
        self.live.emit("calendar")

    def patch_budget(self, provider: str, req: s.BudgetPatch) -> s.ProviderBudget:
        self.budgets.set_budget(provider, req)
        self._budget_changed()
        return self.budgets.summary(provider)

    def boost(self, provider: str, req: s.BoostRequest) -> s.ProviderBudget:
        self.budgets.set_boost(provider, req)
        self._budget_changed()
        return self.budgets.summary(provider)

    def clear_boost(self, provider: str) -> s.ProviderBudget:
        self.budgets.clear_boost(provider)
        self._budget_changed()
        return self.budgets.summary(provider)

    def patch_reserve(self, req: s.ReserveSettings) -> s.BudgetOverview:
        for key, v, lo, hi in (("RESERVE_WEEKLY_PCT", req.reserve_weekly_pct, 0, 100), ("RESERVE_FIVE_HOUR_PCT", req.reserve_five_hour_pct, 0, 100),
                               ("SWEEP_WINDOW_HOURS", req.sweep_window_hours, 0, 48), ("SWEEP_CEILING_PCT", req.sweep_ceiling_pct, 0, 100)):
            if v is not None:
                if not lo <= v <= hi:
                    raise BadRequest("out_of_range", f"{key} must be between {lo} and {hi}.")
                self.settings.set(key, v)
        self._budget_changed()
        return self.budget_overview()

    def _provider_info(self, pid: str) -> s.ProviderInfo:
        r = self.db.q1("SELECT * FROM providers WHERE id=?", (pid,))
        if not r:
            raise NotFound(f"provider {pid}")
        if r["kind"] == SUBSCRIPTION:
            u = self.usage.cached()
            source = u.source if u else "none"
            status = (f"Weekly usage {u.seven_day_pct:.0f}% (read {u.source})" if u and u.seven_day_pct is not None
                      else f"Usage unknown: {(u and u.error) or 'not read yet'}. The agent logs in to Claude on its own computer (see System).")
        elif r["kind"] == OPENCODE_GO:
            source = "ledger"
            status = ("Key saved (write-only); runs on opencode, each step's cost reported by opencode. Go's own limits apply too."
                      if r["credential_enc"] else "No key saved")
        else:
            source = "ledger"
            status = "API key saved (write-only); usage priced from the runtime's reported cost" if r["credential_enc"] else "No API key saved"
        models = self._models(pid)
        return s.ProviderInfo(id=r["id"], kind=r["kind"], label=r["label"], enabled=bool(r["enabled"]), status=status, usage_source=source,
                              has_credential=bool(r["credential_enc"]) or r["kind"] == SUBSCRIPTION, runtime=RUNTIME.get(r["kind"], "claude-code"),
                              models=models)

    def _models(self, pid: str) -> list[s.ProviderModel] | None:
        from ..catalog import models as catalog

        enabled = self.budgets.models(pid)
        if enabled is None:
            return None
        cat = {m["id"]: m for m in catalog(self.db, pid)}
        return [s.ProviderModel(id=m, name=cat.get(m, {}).get("name") or m.split("/", 1)[1], input=cat.get(m, {}).get("input"), output=cat.get(m, {}).get("output"),
                                variants=cat.get(m, {}).get("variants") or [], enabled=True) for m in enabled]

    def provider_catalog(self, provider: str, refresh: bool = False) -> list[s.ProviderModel]:
        from ..catalog import models as catalog

        enabled = self.budgets.models(provider)
        if enabled is None:
            raise BadRequest("no_catalog", "Claude providers use haiku, sonnet and opus.")
        return [s.ProviderModel(id=m["id"], name=m["name"], input=m["input"], output=m["output"], variants=m["variants"], enabled=m["id"] in enabled)
                for m in catalog(self.db, provider, refresh) if m.get("tools", True)]

    def providers(self) -> list[s.ProviderInfo]:
        return [self._provider_info(p) for p in self.budgets.provider_ids(enabled_only=False)]

    def add_api_provider(self, req: s.ApiProviderCreate) -> s.ProviderInfo:
        self.budgets.add_api_provider(req.key, req.label, req.kind)
        if req.use_for_all_roles:
            def moved(r: s.RoleConfig) -> s.RoleConfig:
                model, effort = self.budgets.resolve(req.kind, r.tier)  # sonnet/medium → that-size Go model, same effort
                return r.model_copy(update={"provider": req.kind, "tier": f"{model}/{effort}" if effort else model})
            self.budgets.set_roles([moved(r) for r in self.budgets.roles()])
        self._budget_changed()
        return self._provider_info(req.kind)

    def patch_provider(self, provider: str, req: s.ProviderPatch) -> s.ProviderInfo:
        self._provider_info(provider)
        if req.enabled is False and provider == self.budgets.role("worker").provider and len(self.budgets.provider_ids()) <= 1:
            raise BadRequest("last_provider", "That's the only provider the assistant can use; add another before turning it off.")
        if req.enabled is not None:
            self.db.ex("UPDATE providers SET enabled=?, updated_at=? WHERE id=?", (int(req.enabled), self.db.now(), provider))
        if req.label:
            self.db.ex("UPDATE providers SET label=?, updated_at=? WHERE id=?", (req.label.strip()[:60], self.db.now(), provider))
        if req.models:
            self.budgets.set_models(provider, req.models)
        self._budget_changed()
        return self._provider_info(provider)

    def delete_provider(self, provider: str) -> None:
        if provider == SUBSCRIPTION:
            raise BadRequest("builtin", "The Claude subscription can be turned off, not removed.")
        self._provider_info(provider)
        if any(r.provider == provider for r in self.budgets.roles()):
            self.db.ex("DELETE FROM kv WHERE key='roles'")  # roles fall back to the subscription
        self.db.ex("DELETE FROM providers WHERE id=?", (provider,))
        self._budget_changed()

    def roles(self) -> s.Roles:
        return s.Roles(roles=self.budgets.roles())

    def set_roles(self, req: s.Roles) -> s.Roles:
        self.budgets.set_roles(req.roles)
        self._budget_changed()
        return self.roles()

    def reviewer_config(self) -> s.ReviewerConfig:
        return self.budgets.reviewer_config()

    def set_reviewer_config(self, req: s.ReviewerConfig) -> s.ReviewerConfig:
        self.budgets.set_reviewer_config(req)
        return self.budgets.reviewer_config()

    def instructions(self) -> s.StandingInstructions:
        return self.budgets.instructions()

    def set_instructions(self, req: s.StandingInstructions) -> s.StandingInstructions:
        old = self.budgets.instructions().text
        self.budgets.set_instructions(self.vault.redact(req.text) or "")
        if old.strip() != req.text.strip():
            self.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(NULL, ?, ?)",
                       ("[control] The owner updated your standing instructions; they're in your next brief.", self.db.now()))
        return self.budgets.instructions()

    # ── the calendar and wakeups ───────────────────────────────────────────
    def calendar(self, start: str | None, end: str | None) -> s.Calendar:
        from datetime import timedelta

        from ..budgets import in_hours
        pid = self.budgets.role("worker").provider
        tz = self.budgets.tz()
        now = self.budgets.now()
        loc = now.astimezone(tz)
        a = parse(start) if start else (loc - timedelta(days=loc.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        b = parse(end) if end else a + timedelta(days=7)  # type: ignore[operator]
        if (b - a).days > 42:  # type: ignore[operator]
            raise BadRequest("range", "Ask for at most six weeks at a time.")
        rows = self.db.q("SELECT * FROM wakeups WHERE at>=? AND at<? AND status != 'cancelled' ORDER BY at", (iso(a), iso(b)))  # type: ignore[arg-type]
        windows: list[s.CalendarWindow] = []
        hours = self.budgets.effective(pid, now).limits.allowed_hours
        t, cur = a, None
        while t < b:  # type: ignore[operator]
            inside = in_hours(hours, t.astimezone(tz))  # type: ignore[union-attr]
            if inside and cur is None:
                cur = t
            if not inside and cur is not None:
                windows.append(s.CalendarWindow(kind="allowed", start=cur, end=t))  # type: ignore[arg-type]
                cur = None
            t += timedelta(minutes=30)  # type: ignore[operator]
        if cur is not None:
            windows.append(s.CalendarWindow(kind="allowed", start=cur, end=b))  # type: ignore[arg-type]
        sw = self.scheduler.sweep_window(pid, now)
        if sw and self.budgets.effective(pid, now).limits.sweep and sw[1] > a and sw[0] < b:  # type: ignore[operator]
            windows.append(s.CalendarWindow(kind="sweep", start=sw[0], end=sw[1]))
        runs = [run_model(self.db, r) for r in self.db.q("SELECT * FROM runs WHERE started_at>=? AND started_at<? ORDER BY id", (iso(a), iso(b)))]  # type: ignore[arg-type]
        return s.Calendar(start=a, end=b, timezone=str(tz), wakeups=[wakeup_model(self.db, r) for r in rows], windows=windows,  # type: ignore[arg-type]
                          budget=self.budgets.summary(pid, now), runs=runs)

    def create_wakeup(self, req: s.WakeupCreate) -> s.Wakeup:
        pid = req.provider or self.budgets.role("worker").provider
        return self.scheduler.plan(pid, req.at, req.budget, req.tier, req.reason or "Added by you", by="owner")

    def patch_wakeup(self, wakeup_id: int, req: s.WakeupPatch) -> s.Wakeup:
        return self.scheduler.owner_edit(wakeup_id, req)

    def cancel_wakeup(self, wakeup_id: int) -> s.Wakeup:
        return self.scheduler.cancel(wakeup_id, by="owner")

    # ── routines ───────────────────────────────────────────────────────────
    def routines_list(self) -> list[s.Routine]:
        return self.routines.list()

    def create_routine(self, req: s.RoutineCreate) -> s.Routine:
        r = self.routines.create(req, by="owner")
        self.live.emit("calendar")
        return r

    def patch_routine(self, routine_id: int, req: s.RoutinePatch) -> s.Routine:
        r = self.routines.patch(routine_id, req)
        self.live.emit("calendar")
        return r

    def delete_routine(self, routine_id: int) -> None:
        self.routines.delete(routine_id)
        self.live.emit("calendar")

    # ── system ─────────────────────────────────────────────────────────────
    async def _system(self) -> s.SystemStatus:
        try:
            h = await self.runner.health()
            runner = s.RunnerHealth(reachable=True, busy=h.get("busy"), runtime_version=h.get("runtime_version"), logged_in=h.get("logged_in"))
        except RunnerError as e:
            runner = s.RunnerHealth(reachable=False, error=str(e))
        try:
            st = await self.egress.status()
            egress = s.EgressHealth(reachable=True, lease_active=st.get("lease_active"))
        except RunnerError as e:
            egress = s.EgressHealth(reachable=False, error=str(e))
        reading = await self.usage.read(max_age=120)
        g = self.usage.gate(reading)
        gate = s.GateState(open=g.open, reason=g.reason)
        if self.runs_mgr.active_run_id() is not None:
            gate = s.GateState(open=False, reason="A run is in progress.")
        return s.SystemStatus(server_version=__version__, runner=runner, egress=egress, usage=reading, gate=gate)

    def system_status(self) -> s.SystemStatus:
        return self._await(self._system())

    async def _selftest(self) -> s.SelfTestResult:
        sysst = await self._system()
        checks = [
            s.SelfTestCheck(id="database", label="Database and storage", result="ok", detail=f"schema v{self.db.q1('SELECT MAX(version) AS v FROM schema_version')['v']}"),
            s.SelfTestCheck(id="agent_token", label="Agent token configured", result="ok" if self.env.agent_token else "fail",
                            detail=None if self.env.agent_token else "L0W_AGENT_TOKEN is empty: the agent can't reach its API"),
            s.SelfTestCheck(id="runner", label="Agent computer reachable", result="ok" if sysst.runner.reachable else "fail",
                            detail=sysst.runner.runtime_version if sysst.runner.reachable else sysst.runner.error),
            s.SelfTestCheck(id="egress", label="Egress proxy reachable", result="ok" if sysst.egress.reachable else "fail", detail=sysst.egress.error),
            s.SelfTestCheck(id="provider_login", label="Provider login", result="ok" if sysst.runner.logged_in else "warn",
                            detail="Claude" if sysst.runner.logged_in else "the agent isn't logged in to Claude yet"),
            s.SelfTestCheck(id="usage", label="Usage readable", result="ok" if sysst.usage and sysst.usage.seven_day_pct is not None else "warn",
                            detail=(sysst.usage.error if sysst.usage else None) or (f"{sysst.usage.source}" if sysst.usage else None)),
        ]
        if sysst.runner.reachable:
            targets = {
                "control_owner": f"{self.env_selftest_control()}/api/status",
                "egress_admin": "http://egress:8090/status",
                "proxy": "http://egress:3128",
                "gated_host": "api.anthropic.com",
                "lease_active": bool(sysst.egress.lease_active),
                "lan": [t.strip() for t in str(self.settings.get("SELFTEST_LAN_TARGETS")).split(",") if t.strip()],
            }
            try:
                for c in await self.runner.selftest(targets):
                    checks.append(s.SelfTestCheck(**c))
            except (RunnerError, TypeError, ValueError) as e:
                checks.append(s.SelfTestCheck(id="probes", label="Isolation probes", result="fail", detail=str(e)))
        else:
            checks.append(s.SelfTestCheck(id="probes", label="Isolation probes", result="skip", detail="the agent computer isn't reachable"))
        return s.SelfTestResult(ran_at=utcnow(), ok=not any(c.result == "fail" for c in checks), checks=checks, note=SELFTEST_NOTE)

    def env_selftest_control(self) -> str:
        import os

        return os.environ.get("L0W_SELFTEST_CONTROL_URL", "http://control:8000").rstrip("/")

    def selftest(self) -> s.SelfTestResult:
        return self._await(self._selftest(), timeout=120)


def _work_changes(tool_items: list[dict]) -> list[s.FileChange]:
    """Work-file changes from the run's Write/Edit tool calls under /work (line counts are
    approximate: tool inputs are stored clipped)."""
    tally: dict[str, list[int]] = {}

    def lines(v) -> int:
        return len(str(v).splitlines()) if v else 0

    for item in tool_items:
        d = json.loads(item["data_json"])
        name, inp = d.get("name"), d.get("input") or {}
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if not path.startswith("/work/") or path.startswith("/work/_inbox/") or d.get("is_error"):
            continue
        t = tally.setdefault(path[len("/work/"):], [0, 0])
        if name == "Write":
            t[0] += lines(inp.get("content"))
        elif name == "Edit":
            t[0] += lines(inp.get("new_string"))
            t[1] += lines(inp.get("old_string"))
        elif name == "MultiEdit":
            for e in inp.get("edits") or []:
                t[0] += lines(e.get("new_string"))
                t[1] += lines(e.get("old_string"))
        elif name == "NotebookEdit":
            t[0] += lines(inp.get("new_source"))
    return [s.FileChange(path=p, added=a, removed=r) for p, (a, r) in sorted(tally.items())]
