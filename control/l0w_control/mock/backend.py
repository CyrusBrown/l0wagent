"""In-memory backend over the mockup's fixtures, for building the UI before
the real backend exists. State changes (answers, new messages…) persist until
the process restarts, and are pushed on the live stream like the real thing.

Login (only enforced with L0W_MOCK_LOGIN=1): any username, password
"password"; a username starting with "totp" asks for code 123456.
"""

from __future__ import annotations

import asyncio
import mimetypes
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import __version__
from ..api import schemas as s
from ..backends.base import Backend, BadRequest, Conflict, LoginFailed, NotFound, Unauthorized
from ..integrations import NAME_RE, PRESETS
from . import fixtures

MOCK_PASSWORD = "password"
MOCK_TOTP = "123456"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class MockBackend(Backend):
    def __init__(self, require_login: bool = False, world: fixtures.World | None = None):
        self.w = world or fixtures.build()
        self.require_login = require_login
        self.tokens: set[str] = set()
        self.challenges: dict[str, str] = {}
        self.idem: dict[str, Any] = {}
        self.seq = 0
        self.changes: list[tuple[int, s.SyncChange]] = []
        self.listeners: set[asyncio.Queue[s.LiveEvent]] = set()
        self.owner = s.Owner(username="l0w", display_name="l0w", timezone="America/Los_Angeles", totp_enabled=False, passkeys=0)

    # ── plumbing ───────────────────────────────────────────────────────────
    def _changed(self, entity: str, id_: Any, model: s.Model | None, event: str | None = None) -> None:
        self.seq += 1
        change = s.SyncChange(entity=entity, id=str(id_), op="upsert" if model else "delete", data=model.model_dump(mode="json") if model else None)
        self.changes.append((self.seq, change))
        self._emit(s.LiveEvent(type=event or entity, cursor=str(self.seq), data=change.model_dump(mode="json")))  # type: ignore[arg-type]

    def _emit(self, event: s.LiveEvent) -> None:
        for q in list(self.listeners):
            q.put_nowait(event)

    def _once(self, key: str | None, make):
        if key is None:
            return make()
        if key not in self.idem:
            self.idem[key] = make()
        return self.idem[key]

    def _recount(self) -> None:
        fixtures.recount(self.w)
        self._emit(s.LiveEvent(type="inbox_counts", data=self.inbox_counts().model_dump(mode="json")))

    # ── meta, status, sync, events ─────────────────────────────────────────
    def meta(self) -> s.Meta:
        return s.Meta(server_version=__version__, api_version="0", ui_bundle_version="dev", capabilities=s.Capabilities(hub=self._member.state == "joined", hub_join=True),
                      setup_complete=True, instance_name="l0wagent (mock)")

    def status(self) -> s.Status:
        running = next((r for r in self.w.runs.values() if r.status == "running"), None)
        return s.Status(
            state="working" if running else "asleep", now=running.now if running else None, run_id=running.id if running else None,
            run_used=running.used if running else None, run_budget=running.budget if running else None,
            next_wakeup_at=_now().replace(hour=2, minute=0, second=0) + timedelta(days=1),
            budget=s.BudgetSummary(unit="pts", left=102, allowance=150, used_by_assistant=48, used_by_owner=300, used_by_chat=22, resets_at=_now() + timedelta(days=3)),
            usage=self._usage(), inbox_waiting=self.inbox_counts().waiting, reply_budget_left=14,
            pause=self._pause if self._pause.paused else None,
        )

    def sync(self, since: str | None) -> s.SyncResponse:
        if since is None:
            snapshot = [
                *(s.SyncChange(entity="project", id=p.slug, op="upsert", data=p.model_dump(mode="json")) for p in self.w.projects.values()),
                *(s.SyncChange(entity="thread", id=str(t.id), op="upsert", data=t.model_dump(mode="json")) for t in self.w.threads.values()),
            ]
            return s.SyncResponse(cursor=str(self.seq), changes=snapshot)
        try:
            n = int(since)
        except ValueError:
            return s.SyncResponse(cursor=str(self.seq), changes=[], reset=True)
        return s.SyncResponse(cursor=str(self.seq), changes=[c for seq, c in self.changes if seq > n])

    async def events(self) -> AsyncIterator[s.LiveEvent]:
        q: asyncio.Queue[s.LiveEvent] = asyncio.Queue()
        self.listeners.add(q)
        try:
            yield s.LiveEvent(type="hello", cursor=str(self.seq), data={"status": self.status().model_dump(mode="json")})
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield s.LiveEvent(type="heartbeat")
        finally:
            self.listeners.discard(q)

    # ── auth ───────────────────────────────────────────────────────────────
    def authenticate(self, session_cookie: str | None, bearer: str | None) -> s.Owner:
        if not self.require_login or session_cookie in self.tokens or bearer in self.tokens:
            return self.owner
        raise Unauthorized()

    def _issue(self, req_want: str) -> tuple[s.LoginResult, str | None]:
        token = secrets.token_urlsafe(24)
        self.tokens.add(token)
        if req_want == "device_token":
            return s.LoginResult(status="ok", device_token=token), None
        return s.LoginResult(status="ok"), token

    def login(self, req: s.LoginRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]:
        if req.username == "locked":
            raise LoginFailed(s.LoginError(error="locked", retry_after_s=900), 423)
        if req.password != MOCK_PASSWORD:
            raise LoginFailed(s.LoginError(error="invalid_credentials"))
        if req.username.startswith("totp"):
            challenge = secrets.token_urlsafe(16)
            self.challenges[challenge] = req.want
            return s.LoginResult(status="totp_required", challenge=challenge), None
        return self._issue(req.want)

    def login_totp(self, req: s.TotpRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]:
        want = self.challenges.get(req.challenge)
        if want is None or req.code != MOCK_TOTP:
            raise LoginFailed(s.LoginError(error="invalid_code"))
        del self.challenges[req.challenge]
        return self._issue(want)

    def logout(self, session_cookie: str | None, bearer: str | None) -> None:
        self.tokens.discard(session_cookie or "")
        self.tokens.discard(bearer or "")

    def setup_owner(self, req: s.SetupOwnerRequest) -> s.Owner:
        raise Conflict("already_set_up", "The mock instance already has an owner.")

    def auth_sessions(self, current_cookie: str | None, current_bearer: str | None) -> list[s.AuthSession]:
        return list(self.w.sessions.values())

    def change_password(self, req: s.PasswordChange, current_cookie: str | None, current_bearer: str | None) -> None:
        if req.current_password != MOCK_PASSWORD:
            raise BadRequest("wrong_password", "That isn't your current password (the mock's is \"password\").")

    def totp_setup(self) -> s.TotpSetup:
        secret = "JBSWY3DPEHPK3PXP"
        return s.TotpSetup(secret=secret, otpauth_uri=f"otpauth://totp/l0wagent:{self.owner.username}?secret={secret}&issuer=l0wagent",
                           qr_svg='<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect width="120" height="120" fill="#eee"/></svg>')

    def totp_enable(self, req: s.TotpEnable) -> s.TotpEnabled:
        if req.code != MOCK_TOTP:
            raise BadRequest("invalid_code", "The mock's code is 123456.")
        self.owner = self.owner.model_copy(update={"totp_enabled": True})
        return s.TotpEnabled(**self.owner.model_dump(), recovery_codes=[f"mock{i}-codes" for i in range(10)])

    def totp_disable(self, req: s.TotpDisable) -> s.Owner:
        if req.password != MOCK_PASSWORD:
            raise BadRequest("wrong_password", "That isn't your password.")
        self.owner = self.owner.model_copy(update={"totp_enabled": False})
        return self.owner

    def token_scope(self, bearer: str) -> str:
        return "full"

    def create_device(self, req: s.DeviceCreate, ip: str, user_agent: str) -> s.DeviceCreated:
        return s.DeviceCreated(device_token=secrets.token_urlsafe(24))

    def revoke_session(self, session_id: str) -> None:
        if self.w.sessions.pop(session_id, None) is None:
            raise NotFound(f"session {session_id}")

    # ── projects and threads ───────────────────────────────────────────────
    def projects(self, status: str | None) -> list[s.Project]:
        ps = [p for p in self.w.projects.values() if status is None or p.status == status]
        return sorted(ps, key=lambda p: p.last_activity_at, reverse=True)

    def project(self, slug: str) -> s.Project:
        if slug not in self.w.projects:
            raise NotFound(f"project {slug}")
        return self.w.projects[slug]

    def create_project(self, req: s.ProjectCreate) -> s.Project:
        def make():
            slug = req.slug or "-".join("".join(c if c.isalnum() else " " for c in req.name.lower()).split())[:63] or "project"
            if slug in self.w.projects:
                raise Conflict("slug_taken", slug)
            now = _now()
            p = s.Project(slug=slug, name=req.name, summary=req.summary, status="active", doc_path=None, links=[], created_by="owner",
                          created_at=now, last_activity_at=now, unread=0, open_asks=0, thread_count=0)
            self.w.projects[slug] = p
            self._changed("project", slug, p)
            return p

        return self._once(req.idempotency_key, make)

    def patch_project(self, slug: str, req: s.ProjectPatch) -> s.Project:
        p = self.project(slug)
        for k, v in req.model_dump(exclude_none=True).items():
            setattr(p, k, v)
        self._changed("project", slug, p)
        return p

    def threads(self, project: str | None, general: bool, archived: bool) -> list[s.Thread]:
        ts = [t for t in self.w.threads.values()
              if (archived or t.status == "open")
              and (not general or t.project is None)
              and (project is None or t.project == project)]
        return sorted(ts, key=lambda t: t.last_activity_at, reverse=True)

    def thread(self, thread_id: int) -> s.Thread:
        if thread_id not in self.w.threads:
            raise NotFound(f"thread {thread_id}")
        return self.w.threads[thread_id]

    def create_thread(self, req: s.ThreadCreate) -> s.Thread:
        def make():
            if req.project and req.project not in self.w.projects:
                raise NotFound(f"project {req.project}")
            now = _now()
            tid = max(self.w.threads) + 1
            t = s.Thread(id=tid, project=req.project, title=req.title, type="normal", status="open", created_by="owner", pinned=False,
                         created_at=now, last_activity_at=now, last_message_preview=None, unread=0, open_asks=0)
            self.w.threads[tid] = t
            self._changed("thread", tid, t)
            if req.first_message:
                self.post_message(tid, s.MessageCreate(body=req.first_message))
            fixtures.recount(self.w)
            return t

        return self._once(req.idempotency_key, make)

    def patch_thread(self, thread_id: int, req: s.ThreadPatch) -> s.Thread:
        t = self.thread(thread_id)
        data = req.model_dump(exclude_unset=True)
        if "project" in data:
            if data["project"] and data["project"] not in self.w.projects:
                raise NotFound(f"project {data['project']}")
            t.project = data.pop("project") or None
        for k, v in data.items():
            if v is not None:
                setattr(t, k, v)
        fixtures.recount(self.w)
        self._changed("thread", thread_id, t)
        return t

    def thread_items(self, thread_id: int, before: str | None, limit: int) -> s.ThreadItems:
        self.thread(thread_id)
        w = self.w
        items: list[Any] = [s.MessageItem(at=m.created_at, message=m) for m in w.messages.values() if m.thread_id == thread_id]
        items += [s.QuestionItem(at=x.created_at, question=x) for x in w.questions.values() if x.thread_id == thread_id]
        items += [s.PermissionItem(at=x.created_at, permission=x) for x in w.permissions.values() if x.thread_id == thread_id]
        items += [s.TaskItem(at=x.created_at, task=x) for x in w.tasks.values() if x.thread_id == thread_id]
        items += [s.ClaimItem(at=x.created_at, claim=x) for x in w.claims.values() if x.thread_id == thread_id]
        items += [s.SuggestionItem(at=x.created_at, suggestion=x) for x in w.suggestions.values()
                  if x.target_kind == "thread" and x.target_id == thread_id]
        items += [s.PromoteItem(at=x.created_at, promote=x) for x in w.promotes.values() if x.thread_id == thread_id]
        items.sort(key=lambda i: i.at)
        if before:
            cutoff = datetime.fromisoformat(before)
            items = [i for i in items if i.at < cutoff]
        older = len(items) > limit
        items = items[-limit:]
        return s.ThreadItems(items=items, before_cursor=items[0].at.isoformat() if older and items else None)

    def post_message(self, thread_id: int, req: s.MessageCreate) -> s.Message:
        def make():
            t = self.thread(thread_id)
            now = _now()
            mid = max(self.w.messages, default=0) + 1
            attachments = [s.Attachment(name=u.name, volume="work", path=u.path, size=u.size, mime=u.mime)
                           for uid in req.upload_ids if (u := self.w.uploads.get(uid))]
            m = s.Message(id=mid, thread_id=thread_id, sender="owner", body=req.body, via="web", run_id=None, attachments=attachments,
                          delivery="queued", created_at=now)
            self.w.messages[mid] = m
            t.last_activity_at, t.last_message_preview = now, "You: " + req.body[:80]
            self._changed("message", mid, m)
            self._changed("thread", thread_id, t)
            return m

        return self._once(req.idempotency_key, make)

    def recent_messages(self, sender: str | None, limit: int) -> list[s.RecentMessage]:
        ms = sorted((m for m in self.w.messages.values() if sender is None or m.sender == sender), key=lambda m: m.created_at, reverse=True)[:limit]
        return [s.RecentMessage(message=m, thread_title=self.w.threads[m.thread_id].title, project=self.w.threads[m.thread_id].project) for m in ms]

    def mark_read(self, thread_id: int, message_id: int | None) -> None:
        t = self.thread(thread_id)
        if t.unread:
            t.unread = 0
            fixtures.recount(self.w)
            self._changed("thread", thread_id, t, event="unread")

    def delivery(self, thread_id: int) -> s.Delivery:
        self.thread(thread_id)
        st = self.status()
        if st.state == "working":
            return s.Delivery(state="working", text="Working now, sees it within a minute", next_wakeup_at=None,
                              reply_now_available=False, reply_now_cost=None, reply_budget_left=st.reply_budget_left)
        when = st.next_wakeup_at.strftime("%a %H:%M") if st.next_wakeup_at else "?"
        return s.Delivery(state="queued", text=f"Queued for next wakeup ({when})", next_wakeup_at=st.next_wakeup_at,
                          reply_now_available=True, reply_now_cost=1, reply_budget_left=st.reply_budget_left)

    def reply_now(self, thread_id: int) -> s.Run:
        self.thread(thread_id)
        if any(r.status == "running" for r in self.w.runs.values()):
            raise Conflict("already_running", "The assistant is working now and will see the message within a minute.")
        rid = max(self.w.runs) + 1
        r = s.Run(id=rid, kind="reply", why="owner_message", why_detail=f"Reply now in thread {thread_id}", about=None, now="Reading the thread",
                  provider="claude-subscription", tier="haiku/low", budget=1, used=0, deadline=_now() + timedelta(minutes=10), status="running",
                  started_at=_now(), ended_at=None, projects=[])
        self.w.runs[rid] = r
        self._changed("run", rid, r)
        return r

    # ── inbox and cards ────────────────────────────────────────────────────
    def _entry(self, kind: str, obj: Any) -> s.InboxEntry:
        tid = getattr(obj, "thread_id", None)
        if kind == "suggestion" and obj.target_kind == "thread":
            tid = obj.target_id
        t = self.w.threads.get(tid) if tid else None
        return s.InboxEntry(kind=kind, id=obj.id, at=obj.created_at, thread_id=tid, thread_title=t.title if t else None,
                            project=t.project if t else None, **{kind: obj})  # type: ignore[arg-type]

    def _all_entries(self) -> list[tuple[s.InboxEntry, bool]]:
        w = self.w
        out = []
        for kind, coll in (("question", w.questions), ("permission", w.permissions), ("task", w.tasks),
                           ("suggestion", w.suggestions), ("promote", w.promotes), ("security", w.security)):
            for obj in coll.values():
                if obj.status == "withdrawn":
                    continue
                out.append((self._entry(kind, obj), obj.status == "open"))
        return out

    def inbox(self, view: str, kind: str | None, q: str | None) -> list[s.InboxEntry]:
        waiting = view == "waiting"
        entries = [e for e, is_open in self._all_entries() if is_open == waiting and (kind is None or e.kind == kind)]
        if q:
            ql = q.lower()
            entries = [e for e in entries if ql in e.model_dump_json().lower()]
        return sorted(entries, key=lambda e: e.at, reverse=True)

    def inbox_counts(self) -> s.InboxCounts:
        by: dict[str, int] = {}
        for e, is_open in self._all_entries():
            if is_open:
                by[e.kind] = by.get(e.kind, 0) + 1
        return s.InboxCounts(waiting=sum(by.values()), by_kind=by)  # type: ignore[arg-type]

    def _open(self, coll: dict[int, Any], id_: int, what: str):
        obj = coll.get(id_)
        if obj is None:
            raise NotFound(f"{what} {id_}")
        if obj.status != "open":
            raise Conflict("already_decided", f"{what} {id_} is {obj.status}")
        return obj

    def _decided(self, kind: str, obj: Any) -> None:
        self._recount()
        self._changed(kind, obj.id, obj, event="card")

    def answer_question(self, qid: int, req: s.QuestionAnswer) -> s.Question:
        def make():
            x = self._open(self.w.questions, qid, "question")
            ok = {"yes_no": lambda v: isinstance(v, bool), "multi": lambda v: isinstance(v, list) and all(o in x.options for o in v),
                  "single": lambda v: v in x.options, "slot": lambda v: v in x.options, "text": lambda v: isinstance(v, str) and v.strip() != "",
                  "scale": lambda v: isinstance(v, int) and 1 <= v <= 5}[x.kind]
            if not ok(req.value):
                raise Conflict("invalid_answer", f"not a valid answer for a {x.kind} question")
            x.status, x.answer, x.note, x.answered_at = "answered", req.value, req.note, _now()
            self._decided("question", x)
            return x

        return self._once(req.idempotency_key, make)

    def decide_permission(self, pid: int, req: s.PermissionDecision) -> s.Permission:
        x = self._open(self.w.permissions, pid, "permission")
        if req.decision == "allow" and req.scope not in x.scopes:
            raise Conflict("invalid_scope", f"choose one of {x.scopes}")
        x.status, x.scope, x.note, x.decided_at = ("allowed" if req.decision == "allow" else "denied"), req.scope, req.note, _now()
        self._decided("permission", x)
        return x

    def resolve_task(self, tid: int, req: s.TaskResolve) -> s.Task:
        def make():
            x = self._open(self.w.tasks, tid, "task")
            x.status, x.note, x.resolved_at = req.outcome, req.note, _now()
            self._decided("task", x)
            return x

        return self._once(req.idempotency_key, make)

    def decide_suggestion(self, sid: int, req: s.SuggestionDecision) -> s.Suggestion:
        x = self._open(self.w.suggestions, sid, "suggestion")
        x.status, x.decided_at = ("accepted" if req.decision == "accept" else "dismissed"), _now()
        if x.status == "accepted" and x.kind == "thread_archive":
            self.patch_thread(x.target_id, s.ThreadPatch(status="archived"))
        self._decided("suggestion", x)
        return x

    def accept_all_suggestions(self) -> list[s.Suggestion]:
        return [self.decide_suggestion(x.id, s.SuggestionDecision(decision="accept"))
                for x in list(self.w.suggestions.values()) if x.status == "open"]

    def decide_promote(self, pid: int, req: s.PromoteDecision) -> s.PromoteRequest:
        x = self._open(self.w.promotes, pid, "promote request")
        x.status, x.note, x.decided_at = ("approved" if req.decision == "approve" else "denied"), req.note, _now()
        self._decided("promote", x)
        return x

    def decide_security(self, aid: int, req: s.SecurityDecision) -> s.SecurityAlert:
        x = self._open(self.w.security, aid, "security alert")
        x.status = "confirmed" if req.decision == "confirm" else "revoked"
        if x.status == "revoked":
            self.w.sessions.pop(x.session_id, None)
        self._decided("security", x)
        return x

    def claims(self, active: bool) -> list[s.Claim]:
        return [c for c in self.w.claims.values() if not active or c.status in ("planned", "in_progress")]

    # ── search ─────────────────────────────────────────────────────────────
    def search(self, q: str, kind: str | None, project: str | None, since: str | None) -> list[s.SearchHit]:
        ql, w, hits = q.lower(), self.w, []
        for p in w.projects.values():
            if ql in (p.name + p.summary).lower():
                hits.append(s.SearchHit(kind="project", title=p.name, snippet=p.summary, link=f"l0wagent://project/{p.slug}", project=p.slug, at=p.last_activity_at))
        for t in w.threads.values():
            if ql in t.title.lower():
                hits.append(s.SearchHit(kind="thread", title=t.title, snippet=None, link=f"l0wagent://thread/{t.id}", project=t.project, at=t.last_activity_at))
        for m in w.messages.values():
            i = m.body.lower().find(ql)
            if i >= 0:
                t = w.threads[m.thread_id]
                hits.append(s.SearchHit(kind="message", title=t.title, snippet="…" + m.body[max(0, i - 40): i + 60] + "…",
                                        link=f"l0wagent://thread/{t.id}#m{m.id}", project=t.project, at=m.created_at))
        for r in w.runs.values():
            if ql in (r.about or "").lower():
                hits.append(s.SearchHit(kind="run", title=f"#{r.id} {r.about}", snippet=r.status, link=f"l0wagent://run/{r.id}", project=None, at=r.started_at))
        for path in _walk(w.files):
            if ql in path.rsplit("/", 1)[-1].lower():
                hits.append(s.SearchHit(kind="file", title=path.rsplit("/", 1)[-1], snippet=path, link=f"l0wagent://file/{path}", project=None, at=None))
        return [h for h in hits if (kind is None or h.kind == kind) and (project is None or h.project == project)
                and (since is None or (h.at is not None and h.at.date().isoformat() >= since))]

    # ── runs ───────────────────────────────────────────────────────────────
    def runs(self, kind: str | None, project: str | None, limit: int) -> list[s.Run]:
        rs = [r for r in self.w.runs.values() if (kind is None or r.kind == kind) and (project is None or project in r.projects)]
        return sorted(rs, key=lambda r: r.id, reverse=True)[:limit]

    def run(self, run_id: int) -> s.RunDetail:
        r = self.w.runs.get(run_id)
        if r is None:
            raise NotFound(f"run {run_id}")
        changes = s.RunChanges(memory=[], work=[], shelved=[], asks=[], threads=[])
        if run_id == 123:
            changes = s.RunChanges(
                memory=[s.FileChange(path="projects/garden-irrigation.md", added=12, removed=3), s.FileChange(path="knowledge/esp32-dst.md", added=21, removed=0)],
                work=[s.FileChange(path="garden-irrigation/firmware/scheduler.py", added=18, removed=6), s.FileChange(path="garden-irrigation/firmware/test_dst.py", added=64, removed=0)],
                shelved=["scheduler.py · live"], asks=["Flash the fix?", "Write access"], threads=["Suggested: split calibration"],
            )
        posted: dict[int, s.RunThread] = {}
        for m in sorted((m for m in self.w.messages.values() if m.run_id == run_id and m.sender == "assistant"), key=lambda m: m.created_at, reverse=True):
            th = posted.get(m.thread_id)
            if th:
                th.messages += 1
            else:
                posted[m.thread_id] = s.RunThread(thread_id=m.thread_id, title=self.w.threads[m.thread_id].title, messages=1, last_at=m.created_at)
        return s.RunDetail(run=r, claims=[c for c in self.w.claims.values() if c.run_id == run_id], threads=list(posted.values()), changes=changes,
                           reviewer_mode="monitor", level="steady")

    def timeline(self, run_id: int, view: str, kinds: list[str] | None, after: int | None) -> list[s.TimelineItem]:
        self.run(run_id)
        items = self.w.timeline.get(run_id, [])
        return [i for i in items if (view == "detailed" or i.simple) and (not kinds or i.kind in kinds) and (after is None or i.id > after)]

    def start_run(self, req: s.RunStart) -> s.Run:
        if any(r.status == "running" for r in self.w.runs.values()):
            raise Conflict("already_running", "A run is in progress.")
        rid = max(self.w.runs) + 1
        r = s.Run(id=rid, kind="wakeup", why="manual", why_detail=req.reason, about=None, now="Reading the wakeup brief",
                  provider="claude-subscription", tier=req.tier or "sonnet/medium", budget=req.budget or 5, used=0,
                  deadline=_now() + timedelta(minutes=45), status="running", started_at=_now(), ended_at=None, projects=[])
        self.w.runs[rid] = r
        self._changed("run", rid, r)
        return r

    def add_run_budget(self, run_id: int, req: s.RunBudgetAdd) -> s.Run:
        r = self.run(run_id).run
        if r.status not in ("running", "starting"):
            raise Conflict("not_running", f"run {run_id} is {r.status}")
        r.budget = round((r.budget or 0) + req.add, 2)
        self._changed("run", run_id, r)
        return r

    def stop_run(self, run_id: int) -> s.Run:
        r = self.run(run_id).run
        if r.status != "running":
            raise Conflict("not_running", f"run {run_id} is {r.status}")
        r.status, r.ended_at, r.now = "stopped", _now(), None
        self._changed("run", run_id, r)
        self._emit(s.LiveEvent(type="status", data=self.status().model_dump(mode="json")))
        return r

    def jobs(self, include_done: bool) -> list[s.Job]:
        return [j for j in self.w.jobs.values() if include_done or j.status == "running"]

    def update_info(self) -> s.UpdateInfo:
        return s.UpdateInfo(current="0.4.0", channel="stable", latest="0.5.0", available=True, url="https://github.com/CyrusBrown/l0wagent/releases/tag/v0.5.0",
                            notes="First friends release: install with ./l0w, an installable web app with push notifications, and the l0w CLI.",
                            checked_at=_now() - timedelta(hours=1), error=None, draining_until=None, waiting_for=[])

    # ── hub membership: a fake hub, so the Hub settings screen works on fake data ──
    _member = s.Membership(state="standalone", available=True)
    MOCK_HUB = s.HubInfo(name="Example hub", domain="hub.example", base_url="https://hub.example",
                         policies={"terms": "https://hub.example/terms", "privacy": "https://hub.example/privacy"})

    def _mock_invite(self, invite: str) -> None:
        if not invite.strip().startswith("l0w-invite:https://") or "#" not in invite:
            raise BadRequest("bad_invite", "That isn't an invite. It starts with l0w-invite:")

    def membership(self) -> s.Membership:
        return self._member

    def membership_preview(self, req: s.InvitePreviewRequest) -> s.InvitePreview:
        self._mock_invite(req.invite)
        return s.InvitePreview(hub=self.MOCK_HUB, name_hint="mara", expires=_now() + timedelta(days=7), own_url_allowed=True,
                               example=s.MemberHostnames(instance="mara.hub.example", files="files--mara.hubfiles.example"), two_factor_ready=True)

    def membership_join(self, req: s.JoinRequest) -> s.JoinResult:
        self._mock_invite(req.invite)
        if self._member.state == "joined":
            raise Conflict("already_joined", "This instance is already a member of a hub. Leave it first.")
        name = req.name.strip().lower()
        host = s.MemberHostnames(instance=f"{name}.hub.example", files=f"files--{name}.hubfiles.example")
        self._member = s.Membership(
            state="joined", available=True, address=f"{name}@hub.example", mode=req.mode, hub=self.MOCK_HUB, hostnames=host,
            urls=s.MemberHostnames(instance=f"https://{host.instance}", files=f"https://{host.files}"), joined_at=_now(),
            tunnel=s.TunnelStatus(state="connected", since=_now(), accelerator=s.AcceleratorStatus(state="active", since=_now())),
            accelerator=s.AcceleratorSetting(offered=True, enabled=True), check=s.MembershipCheck(ok=True, at=_now()),
            sign_key=s.MemberKey(kid="k1." + secrets.token_urlsafe(16)[:22], created=_now()))
        return s.JoinResult(membership=self._member, recovery_key="l0w-recovery:" + secrets.token_urlsafe(32))

    def _joined(self) -> s.Membership:
        if self._member.state != "joined":
            raise Conflict("not_joined", "This instance isn't a member of a hub.")
        return self._member

    def membership_check(self) -> s.MembershipCheck:
        m = self._joined()
        m.check = s.MembershipCheck(ok=True, at=_now())
        return m.check

    def membership_accelerator(self, req: s.AcceleratorRequest) -> s.Membership:
        m = self._joined()
        if m.accelerator:
            m.accelerator.enabled = req.enabled
        if m.tunnel:
            m.tunnel.accelerator = s.AcceleratorStatus(state="active" if req.enabled else "off", since=_now())
        return m

    def membership_rotate(self) -> s.Membership:
        m = self._joined()
        m.sign_key = s.MemberKey(kid="k1." + secrets.token_urlsafe(16)[:22], created=_now())
        return m

    def membership_recover(self, req: s.RecoverRequest) -> s.Membership:
        if not req.recovery_key.startswith("l0w-recovery:"):
            raise BadRequest("wrong_recovery_key", "That isn't this instance's recovery key.")
        return self.membership_rotate()

    def membership_leave(self, req: s.LeaveRequest) -> s.Membership:
        m = self._joined()
        if req.confirm.strip().lower() != m.address:
            raise BadRequest("confirm", f"Type your address ({m.address}) to confirm.")
        self._member = s.Membership(state="left", available=True, address=m.address, left_at=_now())
        return self._member

    def self_description(self) -> dict | None:
        return None

    def public_origins(self) -> tuple[str | None, str | None]:
        return None, None

    def check_update(self) -> s.UpdateInfo:
        return self.update_info()

    def cancel_update(self) -> s.UpdateInfo:
        return self.update_info()

    _pause = s.PauseState(paused=False)

    def pause_state(self) -> s.PauseState:
        if self._pause.until and self._pause.until <= _now():
            self._pause = s.PauseState(paused=False)
        return self._pause

    def pause(self, req: s.PauseRequest) -> s.PauseState:
        since = self._pause.since if self._pause.paused else _now()
        self._pause = s.PauseState(paused=True, since=since, until=_now() + timedelta(minutes=req.minutes) if req.minutes else None, skipped=self._pause.skipped)
        for r in self.w.runs.values():
            if r.status == "running":
                r.status, r.ended_at, r.now = "stopped", _now(), None
                self._changed("run", r.id, r)
        self._emit(s.LiveEvent(type="status", data=self.status().model_dump(mode="json")))
        return self._pause

    def resume(self) -> s.PauseState:
        self._pause = s.PauseState(paused=False)
        self._emit(s.LiveEvent(type="status", data=self.status().model_dump(mode="json")))
        return self._pause

    def claude_login_start(self) -> s.ClaudeLoginStart:
        return s.ClaudeLoginStart(url="https://claude.ai/oauth/authorize?code=true&client_id=mock")

    def claude_login_code(self, req: s.ClaudeLoginCode) -> s.ClaudeLoginResult:
        return s.ClaudeLoginResult(ok=req.code == "good-code", message="The assistant is logged in to Claude." if req.code == "good-code" else "The code didn't work.")

    def push_key(self) -> s.PushKey:
        return s.PushKey(public_key="BMock-public-key-for-the-ui")

    def push_subscribe(self, req: s.PushSubscribe) -> s.NotificationSettings:
        return self.notification_settings()

    def push_unsubscribe(self, sub_id: int) -> None:
        pass

    def notification_settings(self) -> s.NotificationSettings:
        from ..notify import EVENTS
        return s.NotificationSettings(events={k: d for k, (_, _, d) in EVENTS.items()}, ntfy_url=None, quiet_start="23:00", quiet_end="07:00",
                                      event_labels={k: f"{label}: {desc}" for k, (label, desc, _) in EVENTS.items()},
                                      subscriptions=[s.PushSubscriptionInfo(id=1, label="Phone (installed app)", created_at=_now() - timedelta(days=3),
                                                                            last_ok_at=_now() - timedelta(hours=2), failures=0)])

    def set_notification_settings(self, req: s.NotificationSettings) -> s.NotificationSettings:
        return req

    def push_test(self) -> s.PushTestResult:
        return s.PushTestResult(sent=1)

    # ── budgets, providers, roles, the reviewer (M4) ───────────────────────
    def _limits(self) -> tuple[str, s.BudgetLimits, list[str]]:
        from dataclasses import asdict

        from ..budgets import LEVELS
        b = self.w.budget
        boost = b.get("boost")
        if boost and boost.until <= _now():
            boost = b["boost"] = None
        level = boost.level if boost else b["level"]
        lim = asdict(LEVELS[level][1])
        custom = []
        if not boost:
            for k, v in b["overrides"].items():
                lim[k] = v
                custom.append(k)
        return level, s.BudgetLimits(**lim), custom

    def _summary(self) -> s.ProviderBudget:
        level, lim, custom = self._limits()
        used = 48.0
        scheduled = sum(w.budget for w in self.w.wakeups.values() if w.status == "planned" and w.kind != "sweep")
        left = max(0.0, lim.allowance - used)
        reset = (_now() + timedelta(days=3)).replace(hour=0, minute=0, second=0)
        days = (reset - _now()).total_seconds() / 86400
        return s.ProviderBudget(
            provider="claude-subscription", kind="claude-subscription", label="Claude subscription", enabled=True, unit="pts", level=self.w.budget["level"],
            effective_level=level, boost=self.w.budget.get("boost"), limits=lim, custom=custom, allowance=lim.allowance, left=left, used_by_assistant=used,  # type: ignore[arg-type]
            used_by_owner=300.0, used_by_chat=22.0, room_below_reserve=430.0, reply_allowance=self.w.budget["reply_pts"], reply_left=min(14.0, self.w.budget["reply_pts"]),
            scheduled=scheduled, week_start=reset - timedelta(days=7), resets_at=reset,
            pace=f"{left - scheduled:.0f} pts unscheduled, {days:.1f} days left: ~{max(0, left - scheduled) / days:.0f}/day" if lim.allowance else "Dormant: no scheduled wakeups",
            native="37% of the week used", pts_per_usd=2.4, fallback_pts=self.w.budget.get("fallback_pts") or 15.0, has_credential=True)

    def budget_overview(self) -> s.BudgetOverview:
        from dataclasses import asdict

        from ..budgets import LEVELS, TIERS
        return s.BudgetOverview(providers=[self._summary()], levels=[s.LevelPreset(name=n, text=t, limits=s.BudgetLimits(**asdict(lim))) for n, (t, lim) in LEVELS.items()],  # type: ignore[arg-type]
                                tiers=list(TIERS), reserve_weekly_pct=80, reserve_five_hour_pct=70, sweep_window_hours=5, sweep_ceiling_pct=95)

    def patch_budget(self, provider: str, req: s.BudgetPatch) -> s.ProviderBudget:
        b = self.w.budget
        if req.level is not None:
            b["level"], b["overrides"] = req.level, {}
        if req.overrides is not None:
            for k, v in req.overrides.model_dump(exclude_unset=True).items():
                if v is None:
                    b["overrides"].pop(k, None)
                else:
                    b["overrides"][k] = v
        if req.reply_pts is not None:
            b["reply_pts"] = req.reply_pts
        if req.fallback_pts is not None:
            b["fallback_pts"] = req.fallback_pts
        self._emit(s.LiveEvent(type="budget"))
        return self._summary()

    def boost(self, provider: str, req: s.BoostRequest) -> s.ProviderBudget:
        until = req.until or (_now() + timedelta(hours=req.hours or 24))
        self.w.budget["boost"] = s.Boost(level=req.level, until=until)
        self._emit(s.LiveEvent(type="budget"))
        return self._summary()

    def clear_boost(self, provider: str) -> s.ProviderBudget:
        self.w.budget["boost"] = None
        self._emit(s.LiveEvent(type="budget"))
        return self._summary()

    def patch_reserve(self, req: s.ReserveSettings) -> s.BudgetOverview:
        return self.budget_overview()

    def providers(self) -> list[s.ProviderInfo]:
        from ..budgets import GO_DEFAULT_MODELS
        out = [s.ProviderInfo(id="claude-subscription", kind="claude-subscription", label="Claude subscription", enabled=True, status="Weekly usage 37% (read oauth)",
                              usage_source="oauth", has_credential=True)]
        if self.w.api_key:
            out.append(s.ProviderInfo(id="anthropic-api", kind="anthropic-api", label="Anthropic API", enabled=True, status="API key saved (write-only)",
                                      usage_source="ledger", has_credential=True))
        if getattr(self.w, "go_key", False):
            from ..catalog import KNOWN_VARIANTS
            ids = getattr(self.w, "go_models", None) or list(GO_DEFAULT_MODELS)
            out.append(s.ProviderInfo(id="opencode-go", kind="opencode-go", label="opencode Go", enabled=True, runtime="opencode",
                                      status="Key saved (write-only); runs on opencode, each step's cost reported by opencode. Go's own limits apply too.",
                                      usage_source="ledger", has_credential=True,
                                      models=[s.ProviderModel(id=m, name=m.split("/", 1)[1], input=0.5, output=2.0, variants=KNOWN_VARIANTS.get(m, [])) for m in ids]))
        return out

    def add_api_provider(self, req: s.ApiProviderCreate) -> s.ProviderInfo:
        if req.kind == "opencode-go":
            self.w.go_key = True
            if req.use_for_all_roles:
                self.w.roles = {name: ("opencode-go", t) for name, (_, t) in self.w.roles.items()}
            return next(p for p in self.providers() if p.id == "opencode-go")
        if not req.key.startswith("sk-ant-"):
            raise BadRequest("bad_key", "That doesn't look like an Anthropic API key (they start with sk-ant-).")
        self.w.api_key = True
        return next(p for p in self.providers() if p.id == "anthropic-api")

    def patch_provider(self, provider: str, req: s.ProviderPatch) -> s.ProviderInfo:
        if req.models and provider == "opencode-go":
            self.w.go_models = list(req.models)
        return next(p for p in self.providers() if p.id == provider)

    def provider_catalog(self, provider: str, refresh: bool = False) -> list[s.ProviderModel]:
        from ..catalog import KNOWN_VARIANTS
        enabled = getattr(self.w, "go_models", None) or ["opencode-go/deepseek-v4.1-flash", "opencode-go/deepseek-v4-pro", "opencode-go/glm-5.3"]
        return [s.ProviderModel(id=m, name=m.split("/", 1)[1], input=0.5, output=2.0, variants=v, enabled=m in enabled) for m, v in KNOWN_VARIANTS.items()]

    def delete_provider(self, provider: str) -> None:
        if provider == "opencode-go":
            self.w.go_key = False
        else:
            self.w.api_key = False

    def roles(self) -> s.Roles:
        from ..budgets import TIERS, Budgets
        _, lim, _ = self._limits()
        return s.Roles(roles=[s.RoleConfig(role=name, provider=p, tier=t, allowed=lim.tiers if name == "worker" else list(TIERS), note=Budgets.ROLE_NOTES[name])  # type: ignore[arg-type]
                              for name, (p, t) in self.w.roles.items()])

    def set_roles(self, req: s.Roles) -> s.Roles:
        for r in req.roles:
            self.w.roles[r.role] = (r.provider, r.tier)
        return self.roles()

    def reviewer_config(self) -> s.ReviewerConfig:
        return self.w.reviewer

    def set_reviewer_config(self, req: s.ReviewerConfig) -> s.ReviewerConfig:
        self.w.reviewer = req
        return req

    def instructions(self) -> s.StandingInstructions:
        return s.StandingInstructions(text=self.w.instructions, reviewer_text=getattr(self.w, "reviewer_instructions", ""), updated_at=self.w.now - timedelta(days=2))

    def set_instructions(self, req: s.StandingInstructions) -> s.StandingInstructions:
        self.w.instructions = req.text
        if req.reviewer_text is not None:
            self.w.reviewer_instructions = req.reviewer_text
        return s.StandingInstructions(text=req.text, reviewer_text=getattr(self.w, "reviewer_instructions", ""), updated_at=_now())

    def advanced_settings(self) -> list[s.AdvancedSetting]:
        from ..config import SPECS, advanced_view

        vals = getattr(self.w, "advanced", None) or {}
        return advanced_view(lambda k: vals.get(k, SPECS[k].default))

    def set_advanced_settings(self, values: dict) -> list[s.AdvancedSetting]:
        from ..config import ADVANCED, SPECS, cast, check_range

        if not hasattr(self.w, "advanced"):
            self.w.advanced = {}
        for k, raw in values.items():
            if k not in ADVANCED:
                raise BadRequest("bad_setting", f"{k} isn't an advanced setting.")
            v = cast(SPECS[k].kind, raw)
            try:
                check_range(k, v)
            except ValueError as e:
                raise BadRequest("bad_setting", str(e)) from None
            self.w.advanced[k] = v
        return self.advanced_settings()

    # ── the calendar, wakeups, routines ────────────────────────────────────
    def calendar(self, start: str | None, end: str | None) -> s.Calendar:
        from ..budgets import in_hours
        now = _now()
        a = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        b = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else a + timedelta(days=7)
        _, lim, _ = self._limits()
        windows, t, cur = [], a, None
        hours = [w.model_dump() for w in lim.allowed_hours]
        while t < b:
            inside = in_hours(hours, t)
            if inside and cur is None:
                cur = t
            if not inside and cur is not None:
                windows.append(s.CalendarWindow(kind="allowed", start=cur, end=t))
                cur = None
            t += timedelta(minutes=30)
        if cur is not None:
            windows.append(s.CalendarWindow(kind="allowed", start=cur, end=b))
        summary = self._summary()
        if lim.sweep:
            windows.append(s.CalendarWindow(kind="sweep", start=summary.resets_at - timedelta(hours=5), end=summary.resets_at))
        return s.Calendar(start=a, end=b, timezone=self.owner.timezone, wakeups=sorted((w for w in self.w.wakeups.values() if a <= w.at < b and w.status != "cancelled"), key=lambda w: w.at),
                          windows=windows, budget=summary, runs=[r for r in self.w.runs.values() if a <= r.started_at < b])

    def create_wakeup(self, req: s.WakeupCreate) -> s.Wakeup:
        wid = max(self.w.wakeups, default=0) + 1
        w = s.Wakeup(id=wid, provider="claude-subscription", at=req.at, budget=req.budget, tier=req.tier or "sonnet/medium", reason=req.reason, kind="owner",
                     created_by="owner", routine_id=None, status="planned", status_reason=None, run_id=None, used=None)
        self.w.wakeups[wid] = w
        self._emit(s.LiveEvent(type="calendar", data={"id": wid}))
        return w

    def patch_wakeup(self, wakeup_id: int, req: s.WakeupPatch) -> s.Wakeup:
        w = self.w.wakeups.get(wakeup_id)
        if not w:
            raise NotFound(f"wakeup {wakeup_id}")
        if w.status != "planned":
            raise BadRequest("not_planned", f"Wakeup #{wakeup_id} is {w.status}, not planned.")
        self.w.wakeups[wakeup_id] = w = w.model_copy(update={k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None})
        self._emit(s.LiveEvent(type="calendar", data={"id": wakeup_id}))
        return w

    def cancel_wakeup(self, wakeup_id: int) -> s.Wakeup:
        w = self.w.wakeups.get(wakeup_id)
        if not w:
            raise NotFound(f"wakeup {wakeup_id}")
        self.w.wakeups[wakeup_id] = w = w.model_copy(update={"status": "cancelled", "status_reason": "cancelled by you"})
        self._emit(s.LiveEvent(type="calendar", data={"id": wakeup_id}))
        return w

    def routines_list(self) -> list[s.Routine]:
        return [r for r in self.w.routines.values() if r.status != "declined"]

    def create_routine(self, req: s.RoutineCreate) -> s.Routine:
        from ..routines import check_schedule
        from ..scheduler import schedule_text
        sch = check_schedule(req.schedule)
        rid = max(self.w.routines, default=0) + 1
        self.w.routines[rid] = r = s.Routine(id=rid, name=req.name, prompt=req.prompt, schedule=sch, schedule_text=schedule_text(sch), provider="claude-subscription",
                                             tier=req.tier, budget=req.budget, enabled=req.enabled, builtin=None, status="active", created_by="owner", next_at=None, last_run_at=None)
        return r

    def patch_routine(self, routine_id: int, req: s.RoutinePatch) -> s.Routine:
        from ..routines import check_schedule
        from ..scheduler import schedule_text
        r = self.w.routines.get(routine_id)
        if not r:
            raise NotFound(f"routine {routine_id}")
        upd = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
        if "schedule" in upd:
            upd["schedule"] = check_schedule(upd["schedule"])
            upd["schedule_text"] = schedule_text(upd["schedule"])
        self.w.routines[routine_id] = r = r.model_copy(update=upd)
        return r

    def delete_routine(self, routine_id: int) -> None:
        r = self.w.routines.get(routine_id)
        if not r:
            raise NotFound(f"routine {routine_id}")
        if r.builtin:
            raise BadRequest("builtin", "The built-in weekly review can be paused, not deleted.")
        del self.w.routines[routine_id]

    # ── files ──────────────────────────────────────────────────────────────
    def _node(self, volume: str, path: str) -> tuple[Any, bool]:
        node, git = self.w.files[volume], False
        git = bool(node.get("_git"))
        for part in [p for p in path.strip("/").split("/") if p]:
            if part in ("..", ".") or not isinstance(node, dict) or part not in node:
                raise NotFound(f"{volume}/{path}")
            node = node[part]
            git = git or (isinstance(node, dict) and bool(node.get("_git")))
        return node, git

    # ── network ────────────────────────────────────────────────────────────
    def network(self, filter: str = "all") -> s.NetworkView:
        log = self.w.egress_log
        if filter == "blocked":
            log = [r for r in log if r.action == "blocked"]
        elif filter in ("get", "post"):
            log = [r for r in log if (r.method or "").lower() == filter]
        elif filter == "hour":
            log = [r for r in log if r.at >= self.w.now - timedelta(hours=1)]
        hosts: dict[str, list[int]] = {}
        for r in self.w.egress_log:
            h = hosts.setdefault(r.host or "", [0, 0])
            h[0] += 1
            h[1] += r.action == "blocked"
        top = sorted(([k, *v] for k, v in hosts.items()), key=lambda x: -x[1])[:6]
        return s.NetworkView(reachable=True, default_policy="allow", rules=sorted(self.w.egress_rules.values(), key=lambda r: r.id), requests=log,
                             requests_24h=361, blocked_24h=3, top_hosts=[s.HostStat(host=h, requests=n, blocked=b) for h, n, b in top])

    def add_egress_rule(self, req: s.EgressRuleCreate) -> s.EgressRule:
        from ..network import check_pattern

        pattern = check_pattern(req.pattern)

        def make() -> s.EgressRule:
            rid = max(self.w.egress_rules, default=0) + 1
            self.w.egress_rules[rid] = s.EgressRule(id=rid, pattern=pattern, action=req.action, note=req.note.strip(), created_at=self.w.now, hits_7d=0)
            return self.w.egress_rules[rid]
        return self._once(req.idempotency_key, make)

    # ── the MCP connector ──────────────────────────────────────────────────
    def connect_request(self, params: dict[str, str]) -> s.ConnectRequest:
        from ..connector import SCOPES
        if not params.get("client_id"):
            raise BadRequest("invalid_client", "Unknown client_id: register first.")
        asked = [x for x in (params.get("scope") or "read").split() if x in SCOPES] or ["read"]
        return s.ConnectRequest(client_name="Claude Desktop", redirect_host="claude.ai",
                                scopes=[s.ConnectorScopeInfo(scope=x, description=SCOPES[x]) for x in asked])  # type: ignore[arg-type]

    def connect_consent(self, req: s.ConnectConsent) -> s.ConnectRedirect:
        state = f"&state={req.params['state']}" if req.params.get("state") else ""
        base = req.params.get("redirect_uri") or "https://claude.ai/api/mcp/auth_callback"
        if not req.approve:
            return s.ConnectRedirect(redirect=f"{base}?error=access_denied{state}")
        self.w.connected_apps.append(s.ConnectedApp(id=max((a.id for a in self.w.connected_apps), default=0) + 1, name="Claude Desktop",
                                                    scopes=req.scopes or ["read"], chat_id=None, created_at=_now(), last_used_at=None))
        return s.ConnectRedirect(redirect=f"{base}?code=mock-code{state}")

    def connected_apps(self) -> list[s.ConnectedApp]:
        return list(self.w.connected_apps)

    def revoke_connected_app(self, app_id: int) -> None:
        before = len(self.w.connected_apps)
        self.w.connected_apps = [a for a in self.w.connected_apps if a.id != app_id]
        if len(self.w.connected_apps) == before:
            raise NotFound(f"connected app {app_id}")

    def delete_egress_rule(self, rule_id: int) -> None:
        if self.w.egress_rules.pop(rule_id, None) is None:
            raise NotFound(f"rule {rule_id}")

    # ── integrations ───────────────────────────────────────────────────────
    def _integration(self, name: str) -> s.Integration:
        i = self.w.integrations.get((name or "").strip().lower())
        if not i:
            raise NotFound(f"integration {name}")
        return i

    def list_integrations(self) -> list[s.Integration]:
        return sorted(self.w.integrations.values(), key=lambda i: i.title)

    # extensions: none in the mock (the real ones need control's process)
    def extensions(self) -> list[s.ExtensionInfo]:
        return []

    def rescan_extensions(self) -> list[s.ExtensionInfo]:
        return []

    def set_extension_enabled(self, name: str, enabled: bool) -> s.ExtensionInfo:
        raise NotFound(f"extension {name}")

    def set_extension_settings(self, name: str, values: dict) -> s.ExtensionInfo:
        raise NotFound(f"extension {name}")

    def extension_ui_file(self, name: str, path: str) -> str:
        raise NotFound(f"extension {name}")

    def extension_cards(self, status: str | None) -> list[s.ExtensionCard]:
        return []

    def resolve_extension_card(self, card_id: int, req: s.ExtensionCardResolve) -> s.ExtensionCard:
        raise NotFound(f"extension card {card_id}")

    def integration_presets(self) -> list[s.IntegrationPreset]:
        return [s.IntegrationPreset(id=k, **{f: x for f, x in v.items() if f in s.IntegrationPreset.model_fields}) for k, v in PRESETS.items()]

    def create_integration(self, req: s.IntegrationCreate) -> s.Integration:
        def make() -> s.Integration:
            p = PRESETS.get(req.preset or "", {}) if req.preset else {}
            if req.preset and not p:
                raise BadRequest("bad_preset", f"Unknown preset {req.preset!r}.")
            name = (req.name or req.preset or "").strip().lower()
            if not NAME_RE.match(name):
                raise BadRequest("bad_name", "Integration names look like github or home-assistant: lowercase letters, digits and -, 2–32 characters.")
            if name in self.w.integrations:
                raise Conflict("exists", f"An integration named {name} already exists.")
            url = req.url or p.get("url", "")
            if not url.startswith(("http://", "https://")):
                raise BadRequest("bad_url", "The URL must start with http:// or https://.")
            secret = (req.auth_secret or p.get("auth_secret") or "").strip().upper() or None
            i = s.Integration(name=name, title=(req.title or p.get("title") or name).strip()[:80], preset=req.preset, url=url, auth_secret=secret,
                              secret_saved=bool(secret and secret in self.w.secrets), enabled=True, status="new", last_error=None, synced_at=None,
                              created_at=self.w.now, tools=[], recent_calls=[])
            self.w.integrations[name] = i
            return i
        return self._once(req.idempotency_key, make)

    def get_integration(self, name: str) -> s.Integration:
        return self._integration(name)

    def update_integration(self, name: str, req: s.IntegrationPatch) -> s.Integration:
        i = self._integration(name)
        d = req.model_dump(exclude_unset=True, exclude_none=True)
        if "auth_secret" in d:
            d["auth_secret"] = d["auth_secret"].strip().upper() or None
            d["secret_saved"] = bool(d["auth_secret"] and d["auth_secret"] in self.w.secrets)
        if "title" in d:
            d["title"] = d["title"].strip()[:80] or i.name
        if "url" in d and not d["url"].startswith(("http://", "https://")):
            raise BadRequest("bad_url", "The URL must start with http:// or https://.")
        self.w.integrations[i.name] = i.model_copy(update=d)
        return self.w.integrations[i.name]

    def delete_integration(self, name: str) -> None:
        del self.w.integrations[self._integration(name).name]

    def sync_integration(self, name: str) -> s.Integration:
        i = self._integration(name)
        self.w.integrations[i.name] = i.model_copy(update={"status": "ok", "last_error": None, "synced_at": self.w.now})
        return self.w.integrations[i.name]

    def set_integration_tool_policy(self, name: str, tool: str, policy: str) -> s.Integration:
        i = self._integration(name)
        if not any(t.name == tool for t in i.tools):
            raise NotFound(f"tool {tool}")
        tools = [t.model_copy(update={"policy": policy}) if t.name == tool else t for t in i.tools]
        self.w.integrations[i.name] = i.model_copy(update={"tools": tools})
        return self.w.integrations[i.name]

    # ── secrets ────────────────────────────────────────────────────────────
    # ── 1:1 chats ──────────────────────────────────────────────────────────
    def _chat(self, chat_id: int) -> s.Chat:
        c = self.w.chats.get(chat_id)
        if not c:
            raise NotFound(f"chat {chat_id}")
        return c

    def _chat_add(self, chat_id: int, role: str, body: str = "", data: dict | None = None) -> s.ChatMessage:
        msgs = self.w.chat_messages.setdefault(chat_id, [])
        m = s.ChatMessage(id=max([x.id for v in self.w.chat_messages.values() for x in v] or [0]) + 1, chat_id=chat_id, role=role, body=body,  # type: ignore[arg-type]
                          data=data or {}, created_at=_now())
        msgs.append(m)
        self.w.chats[chat_id] = self._chat(chat_id).model_copy(update={"updated_at": _now(), "last_message_preview": body[:140] or None})
        return m

    def chats(self, archived: bool) -> list[s.Chat]:
        return sorted((c for c in self.w.chats.values() if (c.status == "archived") == archived), key=lambda c: c.updated_at, reverse=True)

    def chat(self, chat_id: int) -> s.Chat:
        return self._chat(chat_id)

    def create_chat(self, req: s.ChatCreate) -> s.Chat:
        cid = max(self.w.chats or [0]) + 1
        self.w.chats[cid] = s.Chat(id=cid, title=(req.title or req.message.splitlines()[0])[:80], provider=req.provider or "claude-subscription",
                                   tier=req.tier or "sonnet/medium", status="open", busy=False, available=True, created_at=_now(), updated_at=_now())
        self.send_chat_message(cid, s.ChatMessageCreate(body=req.message))
        return self._chat(cid)

    def patch_chat(self, chat_id: int, req: s.ChatPatch) -> s.Chat:
        self.w.chats[chat_id] = self._chat(chat_id).model_copy(update=req.model_dump(exclude_none=True))
        return self._chat(chat_id)

    def chat_messages(self, chat_id: int) -> list[s.ChatMessage]:
        self._chat(chat_id)
        return list(self.w.chat_messages.get(chat_id, []))

    def send_chat_message(self, chat_id: int, req: s.ChatMessageCreate) -> s.ChatMessage:
        m = self._chat_add(chat_id, "owner", req.body)
        self._chat_add(chat_id, "assistant", f"(mock) You said: {req.body}", {"calls": []})
        return m

    def stop_chat(self, chat_id: int) -> s.Chat:
        return self._chat(chat_id)

    def decide_chat_card(self, chat_id: int, message_id: int, req: s.ChatCardDecision) -> s.ChatMessage:
        msgs = self.w.chat_messages.get(chat_id, [])
        i = next((i for i, m in enumerate(msgs) if m.id == message_id and m.role == "card"), None)
        if i is None:
            raise NotFound(f"card {message_id}")
        data = {**msgs[i].data, "state": "done" if req.decision == "approve" else "declined"}
        note = "The owner saved the secret." if req.decision == "approve" else "The owner declined the card."
        if req.decision == "approve" and data.get("kind") == "integration":
            secret = data["secret_name"]
            if secret not in self.w.secrets and not req.value:
                raise BadRequest("no_value", "Paste the token to connect.")
            if req.value:
                self.set_secret(secret, s.SecretSet(value=req.value, scope="integrations"))
            try:
                integ = self.get_integration(data["name"])
            except NotFound:
                integ = self.create_integration(s.IntegrationCreate(name=data["name"], title=data["title"], preset=data.get("preset"), url=data["url"],
                                                                    auth_secret=secret, idempotency_key=None))
            integ = integ.model_copy(update={"status": "ok", "secret_saved": True, "last_error": None})
            self.w.integrations[integ.name] = integ
            data.update(integration=integ.name, status="ok", tool_count=len(integ.tools), last_error=None)
            note = f"Connected {data['title']}: {len(integ.tools)} tools."
        msgs[i] = msgs[i].model_copy(update={"data": data})
        self._chat_add(chat_id, "system", note)
        return msgs[i]

    def chat_handoff(self, chat_id: int, req: s.ChatHandoff) -> s.Chat:
        t = self.create_thread(s.ThreadCreate(title=req.title or self._chat(chat_id).title, project=req.project, first_message=req.summary or "(from a chat)"))
        self.w.chats[chat_id] = self._chat(chat_id).model_copy(update={"handed_off_to": s.ThreadRef(id=t.id, title=t.title)})
        self._chat_add(chat_id, "system", f"Continued in the thread #{t.id} “{t.title}”.", {"thread_id": t.id})
        return self._chat(chat_id)

    def chat_ask(self, chat_id: int, req: s.ChatAsk) -> s.ChatMessage:
        t = self.thread(req.thread_id)
        return self._chat_add(chat_id, "system", f"Asked the assistant in #{t.id} “{t.title}”.", {"thread_id": t.id})

    def chat_pull(self, chat_id: int, req: s.ChatPull) -> s.Chat:
        c = self._chat(chat_id)
        if req.thread_id not in c.context_threads:
            self.w.chats[chat_id] = c.model_copy(update={"context_threads": [*c.context_threads, req.thread_id]})
        return self._chat(chat_id)

    def upload_chat_file(self, name: str, data: bytes, mime: str | None) -> s.ChatFile:
        files = self.w.__dict__.setdefault("chat_files", {})
        fid = len(files) + 1
        files[fid] = (data, name, mime or "application/octet-stream")
        return s.ChatFile(id=fid, name=name, mime=mime or "application/octet-stream", size=len(data))

    def chat_file(self, file_id: int) -> tuple[bytes, str, str]:
        f = self.w.__dict__.get("chat_files", {}).get(file_id)
        if not f:
            raise NotFound(f"file {file_id}")
        return f

    def secrets(self) -> list[s.SecretInfo]:
        return sorted(self.w.secrets.values(), key=lambda x: x.name)

    def set_secret(self, name: str, req: s.SecretSet) -> s.SecretInfo:
        name = name.strip().upper()
        now = _now()
        old = self.w.secrets.get(name)
        if old is None and req.value is None:
            raise NotFound(f"secret {name}")
        scope = req.scope or (old.scope if old else "agent")
        label = {"agent": "All projects", "integrations": "Integrations only"}.get(scope) or next(
            (p.name for p in self.w.projects.values() if f"project:{p.slug}" == scope), None) or next(
            (r.name for r in self.w.routines.values() if f"routine:{r.id}" == scope), None)
        if label is None:
            raise BadRequest("bad_scope", "A secret's scope is 'agent', 'integrations', 'project:<slug>' or 'routine:<id>'.")
        info = s.SecretInfo(name=name, description=req.description if req.description is not None else (old.description if old else ""), scope=scope, scope_label=label,
                            created_at=old.created_at if old else now, updated_at=now, reads=old.reads if old else 0, last_read_at=old.last_read_at if old else None)
        self.w.secrets[name] = info
        return info

    def delete_secret(self, name: str) -> None:
        if self.w.secrets.pop(name.strip().upper(), None) is None:
            raise NotFound(f"secret {name}")

    # ── system ─────────────────────────────────────────────────────────────
    def _usage(self) -> s.UsageReading:
        now = _now()
        return s.UsageReading(provider="claude-subscription", source="oauth", fetched_at=now, five_hour_pct=18.0, five_hour_resets_at=now + timedelta(hours=3),
                              seven_day_pct=41.0, seven_day_resets_at=now + timedelta(days=4), error=None)

    def system_status(self) -> s.SystemStatus:
        return s.SystemStatus(server_version=__version__, runner=s.RunnerHealth(reachable=True, busy=False, runtime_version="2.9.0 (Claude Code)", logged_in=True),
                              egress=s.EgressHealth(reachable=True, lease_active=False), usage=self._usage(),
                              gate=s.GateState(open=False, reason="Everything is paused.") if self.pause_state().paused else s.GateState(open=True),
                              storage=s.StorageStatus(disk_free_bytes=212 * 1024**3, disk_total_bytes=500 * 1024**3, warn_free_gb=5, low=False, db_bytes=18 * 1024**2, measured_at=_now(),
                                                      volumes=[s.VolumeUsage(name="workspace", bytes=42 * 1024**2), s.VolumeUsage(name="work", bytes=3 * 1024**3),
                                                               s.VolumeUsage(name="cache", note="Not visible to control (it belongs to the agent computer).")]))

    def selftest(self) -> s.SelfTestResult:
        rows = [("database", "Database and storage", "ok", "schema v2"), ("runner", "Agent computer reachable", "ok", "2.9.0 (Claude Code)"),
                ("provider_login", "Provider login", "ok", "Claude"), ("no_direct_internet", "No direct internet (only through egress)", "ok", None),
                ("no_lan", "Can't reach the LAN or the host", "ok", "checked 3 addresses"), ("owner_api_refused", "Control's owner API refuses the agent", "ok", "HTTP 403"),
                ("gated_without_lease", "Model provider blocked without a lease", "ok", "HTTP/1.1 403 Forbidden"),
                ("internet_via_egress", "Internet works through egress", "warn", "example.com unreachable")]
        checks = [s.SelfTestCheck(id=i, label=label, result=r, detail=d) for i, label, r, d in rows]  # type: ignore[arg-type]
        return s.SelfTestResult(ran_at=_now(), ok=True, checks=checks, note="Mock self-test.")

    def list_dir(self, volume: str, path: str) -> s.Listing:
        node, git = self._node(volume, path)
        if "size" in node:
            raise Conflict("not_a_folder", path)
        entries = []
        for name, child in node.items():
            if name.startswith("_git"):
                continue
            is_dir = "size" not in child
            entries.append(s.FileEntry(name=name, dir=is_dir, size=None if is_dir else child["size"], modified_at=None if is_dir else child["at"],
                                       last_run_id=None if is_dir else child["run"], from_owner=False if is_dir else child["owner"]))
        entries.sort(key=lambda e: (not e.dir, e.name.lower()))
        return s.Listing(volume=volume, path=path.strip("/"), git=git, entries=entries)  # type: ignore[arg-type]

    def stream_file(self, volume: str, path: str):
        data, mime = self.read_file(volume, path)
        return iter([data]), len(data), mime

    def file_stat(self, volume: str, path: str) -> tuple[int, str]:
        data, mime = self.read_file(volume, path)
        return len(data), mime

    def file_range(self, volume: str, path: str, start: int, end: int) -> bytes:
        return self.read_file(volume, path)[0][start:end + 1]

    def read_file(self, volume: str, path: str) -> tuple[bytes, str]:
        node, _ = self._node(volume, path)
        if "size" not in node:
            raise Conflict("is_a_folder", path)
        key = f"{volume}/{path.strip('/')}"
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        text = self.w.file_text.get(key)
        if text is None:
            text = f"(mock) {path.rsplit('/', 1)[-1]}: {node['size']} bytes. The mock server only has contents for a few files.\n"
            if not mime.startswith("text/") and mime not in ("application/json",):
                mime = "text/plain"
        return text.encode(), mime

    def file_history(self, volume: str, path: str) -> list[s.FileHistoryEntry]:
        node, git = self._node(volume, path)
        if not git:
            return []
        now = _now()
        return [
            s.FileHistoryEntry(rev="a1b2c3d", run_id=123, at=now - timedelta(minutes=20), added=12, removed=3, message="run #123: DST fix notes"),
            s.FileHistoryEntry(rev="9f8e7d6", run_id=119, at=now - timedelta(days=5), added=5, removed=1, message="run #119: traced double trigger"),
            s.FileHistoryEntry(rev="1a2b3c4", run_id=114, at=now - timedelta(days=10), added=30, removed=0, message="run #114: created"),
        ]

    def shelf(self, project: str) -> list[s.ShelfItem]:
        self.project(project)
        return sorted((i for i in self.w.shelf.values() if i.project == project), key=lambda i: i.updated_at, reverse=True)

    def add_to_shelf(self, project: str, req: s.ShelfCreate, by: str) -> s.ShelfItem:
        def make():
            self.project(project)
            node, _ = self._node(req.volume, req.path)
            if "size" not in node:
                raise Conflict("is_a_folder", req.path)
            now = _now()
            sid = max(self.w.shelf, default=0) + 1
            item = s.ShelfItem(id=sid, project=project, label=req.label or req.path.rsplit("/", 1)[-1], volume=req.volume, path=req.path.strip("/"),
                               mode=req.mode, keep_versions=req.keep_versions, versions=1 if req.mode == "snapshot" else 0, added_by=by,  # type: ignore[arg-type]
                               run_id=None, created_at=now, updated_at=now, size=node["size"], mime=mimetypes.guess_type(req.path)[0])
            self.w.shelf[sid] = item
            self._changed("shelf", sid, item)
            return item

        return self._once(req.idempotency_key, make)

    def file_diff(self, volume: str, path: str, rev: str) -> s.FileDiff:
        h = next((x for x in self.file_history(volume, path) if x.rev == rev), None)
        if h is None:
            raise NotFound(f"revision {rev}")
        patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,4 @@\n # Notes\n-Pump runs at 06:00 local.\n+Pump runs at 06:00 UTC (DST-safe).\n+Debounce: 10 min.\n"
        return s.FileDiff(rev=h.rev, parent=None, at=h.at, message=h.message, run_id=h.run_id, added=h.added, removed=h.removed, patch=patch)

    def zip_dir(self, volume: str, path: str):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "(mock zip)")
        buf.seek(0)
        return buf

    def preview_url(self, volume: str, path: str, files_origin: str) -> s.PreviewUrl:
        from urllib.parse import quote

        self._node(volume, path)
        return s.PreviewUrl(url=f"/api/files/raw?volume={volume}&path={quote(path)}", expires_at=_now() + timedelta(hours=1))

    def shelf_raw(self, item_id: int, version: int | None) -> tuple[bytes, str, str]:
        item = self.w.shelf.get(item_id)
        if item is None:
            raise NotFound(f"shelf item {item_id}")
        data, mime = self.read_file(item.volume, item.path)
        return data, mime, item.path.rsplit("/", 1)[-1]

    def shelf_preview_url(self, item_id: int, version: int | None, files_origin: str) -> s.PreviewUrl:
        if item_id not in self.w.shelf:
            raise NotFound(f"shelf item {item_id}")
        return s.PreviewUrl(url=f"/api/shelf/{item_id}/raw", expires_at=_now() + timedelta(hours=1))

    def remove_from_shelf(self, item_id: int, by: str = "owner") -> None:
        if self.w.shelf.pop(item_id, None) is None:
            raise NotFound(f"shelf item {item_id}")
        self._changed("shelf", item_id, None)

    def shelf_versions(self, item_id: int) -> list[s.ShelfVersion]:
        item = self.w.shelf.get(item_id)
        if item is None:
            raise NotFound(f"shelf item {item_id}")
        return [s.ShelfVersion(n=n, size=item.size or 0, run_id=item.run_id, created_at=item.updated_at - timedelta(days=item.versions - n))
                for n in range(item.versions, 0, -1)]

    def upload(self, project: str | None, name: str, data: bytes, mime: str | None, idempotency_key: str | None) -> s.Upload:
        def make():
            safe = name.replace("\\", "/").rsplit("/", 1)[-1].lstrip(".") or "upload"
            folder = self.w.files["work"]
            if project:
                self.project(project)
                folder = folder.setdefault(project, {})
            inbox = folder.setdefault("_inbox", {})
            inbox[safe] = {"size": len(data), "at": _now(), "run": None, "owner": True}
            uid = max(self.w.uploads, default=0) + 1
            path = f"{project + '/' if project else ''}_inbox/{safe}"
            if (mime or "").startswith("text/"):
                self.w.file_text[f"work/{path}"] = data.decode(errors="replace")
            u = s.Upload(id=uid, project=project, name=safe, path=path, size=len(data), mime=mime, created_at=_now())
            self.w.uploads[uid] = u
            self._changed("upload", uid, u)
            return u

        return self._once(idempotency_key, make)


def _walk(tree: dict, prefix: str = "") -> list[str]:
    out = []
    for vol_or_name, child in tree.items():
        if vol_or_name.startswith("_git"):
            continue
        path = f"{prefix}/{vol_or_name}" if prefix else vol_or_name
        if isinstance(child, dict) and "size" not in child:
            out += _walk(child, path)
        else:
            out.append(path)
    return out
