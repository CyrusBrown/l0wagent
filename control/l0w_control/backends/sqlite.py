"""The real backend: SQLite, conversations, the run manager, the usage gate, the vault.

M1: accounts, meta/status/sync/events, runs and timelines, jobs, the system page and
self-test. M2: projects, threads, messages, asks, claims, suggestions, the inbox, Reply
now, search, uploads into _inbox/ and write-only secrets. Files and the shelf are M3 (501).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from collections.abc import AsyncIterator, Coroutine
from typing import Any, TypeVar

from .. import __version__
from ..api import schemas as s
from ..auth import Auth, parse, utcnow
from ..config import Env, Settings
from ..conv import Conversations
from ..db import Database
from ..live import Live
from ..runs.clients import EgressClient, RunnerClient, RunnerError
from ..runs.manager import RunManager, run_model
from ..runs.usage import Usage
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
        self.runs_mgr = RunManager(self.env, self.db, self.settings, self.live, self.runner, self.egress, self.usage, self.conv, self.vault)
        self.inbox_dir = os.environ.get("L0W_INBOX_DIR", "/work/_inbox")
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
        self.live.bind(self.loop)
        await self.runs_mgr.startup()

    async def shutdown(self) -> None:
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
        return s.Status(
            state="working" if run else "dormant", now=run.now if run else None, run_id=run_id, run_used=run.used if run else None,
            run_budget=run.budget if run else None, next_wakeup_at=None, budget=None, usage=self.usage.cached(),
            inbox_waiting=self.inbox_counts().waiting, reply_budget_left=None,
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
        self.db.ex("INSERT INTO notifications(kind, ref_kind, ref_id, title, created_at) VALUES('new_device','security_alert',?,?,?)", (str(aid), text, now))
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
        return self.conv.decide_promote(pid, req)

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
        gate = self.usage.gate(self.usage.cached())
        return s.Delivery(state="queued", text="Queued for the assistant's next run", next_wakeup_at=None,
                          reply_now_available=gate.open, reply_now_cost=None, reply_budget_left=None)

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

    # ── secrets ────────────────────────────────────────────────────────────
    def _secret(self, name: str) -> s.SecretInfo:
        r = next((x for x in self.vault.listing() if x["name"] == name.upper()), None)
        if not r:
            raise NotFound(f"secret {name}")
        return s.SecretInfo(name=r["name"], description=r["description"], scope=r["scope"], created_at=parse(r["created_at"]),  # type: ignore[arg-type]
                            updated_at=parse(r["updated_at"]), reads=r["reads"], last_read_at=parse(r["last_read_at"]))  # type: ignore[arg-type]

    def secrets(self) -> list[s.SecretInfo]:
        return [self._secret(r["name"]) for r in self.vault.listing()]

    def set_secret(self, name: str, req: s.SecretSet) -> s.SecretInfo:
        out = self.vault.set(name, req.value, req.description)
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
        return s.RunDetail(run=run, claims=claims, changes=s.RunChanges(memory=memory, work=[], shelved=[], asks=asks, threads=threads), reviewer_mode="off", level=None)

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
        rows = self.db.q("SELECT * FROM jobs WHERE (? OR status='running') ORDER BY id DESC LIMIT 100", (int(include_done),))
        return [s.Job(id=r["id"], run_id=r["run_id"], label=r["label"], progress=r["progress"], expected_end=parse(r["expected_end"]), dont_interrupt=bool(r["dont_interrupt"]),
                      status=r["status"], started_at=parse(r["started_at"]), log_tail="") for r in rows]  # type: ignore[arg-type]

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
