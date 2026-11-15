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
from ..backends.base import Backend, Conflict, LoginFailed, NotFound, Unauthorized
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
        self.owner = s.Owner(username="mara", display_name="Mara", timezone="Europe/Berlin", totp_enabled=False, passkeys=0)

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
        return s.Meta(server_version=__version__, api_version="0", ui_bundle_version="dev", capabilities=s.Capabilities(),
                      setup_complete=True, instance_name="l0wagent (mock)")

    def status(self) -> s.Status:
        running = next((r for r in self.w.runs.values() if r.status == "running"), None)
        return s.Status(
            state="working" if running else "asleep", now=running.now if running else None, run_id=running.id if running else None,
            run_used=running.used if running else None, run_budget=running.budget if running else None,
            next_wakeup_at=_now().replace(hour=2, minute=0, second=0) + timedelta(days=1),
            budget=s.BudgetSummary(unit="pts", left=42, allowance=120, used_by_assistant=48, used_by_owner=30, resets_at=_now() + timedelta(days=3)),
            inbox_waiting=self.inbox_counts().waiting, reply_budget_left=14,
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
        return s.RunDetail(run=r, claims=[c for c in self.w.claims.values() if c.run_id == run_id], changes=changes,
                           reviewer_mode="monitor", level="Standard")

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

    def remove_from_shelf(self, item_id: int) -> None:
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
