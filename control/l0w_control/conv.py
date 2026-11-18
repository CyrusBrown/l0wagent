"""Conversations: projects, threads, messages, asks, claims and suggestions.

Shared by the owner API (backends/sqlite.py) and the agent API (api/agent.py). The rules:
- The owner decides; the agent proposes. The agent can create threads and projects, post,
  file asks, claim threads and suggest changes to existing threads and projects, and
  withdraw its own asks. Nothing it calls here answers, decides or resolves anything.
- Everything stored from either side goes through `vault.redact`.
- Every change to a cached entity goes through `live.changed`, which feeds the live stream
  and the sync feed.
- What the owner says or decides reaches the agent through `deliver()`: mid-run after its
  next tool call (the hook), from `assistant inbox`/`wait`, or in the next run's brief.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from .api import schemas as s
from .auth import iso, parse
from .backends.base import BadRequest, Conflict, NotFound
from .config import SPECS, Settings, cast, check_range
from .db import Database
from .live import Live
from .vault import Vault

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PREVIEW = 90
SUGGESTION_KINDS = {
    "thread_archive": "thread", "thread_rename": "thread", "thread_move": "thread", "thread_split": "thread", "thread_merge": "thread",
    "project_split": "project", "project_merge": "project", "project_stale": "project", "project_archive": "project",
}
SCOPE_ONCE_NOTE = "once = undone when the run that asked ends, or the next run if it already has"


def slugify(name: str) -> str:
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:63].strip("-")
    return slug or "project"


def _j(v: str | None, default: Any = None) -> Any:
    return json.loads(v) if v else default


CLAIM_FIRST = ("If a message asks you to do something (more than an answer), claim its thread before you start: "
               "`assistant claim N \"plan\"` posts the card that tells the owner you've picked it up and what you'll do. "
               "Close it with `assistant claim done N \"result\"` when it's finished.")


class Conversations:
    def __init__(self, db: Database, live: Live, vault: Vault, settings: Settings):
        self.db, self.live, self.vault, self.settings = db, live, vault, settings
        self.fire: Callable[[str, dict], None] = lambda event, data: None  # extensions' event hooks (set by the backend)
        # Set by the backend: applies an egress-rule action through the egress admin API.
        self.add_egress_rule: Callable[[str, str, str], int] | None = None
        self.delete_egress_rule: Callable[[int], None] | None = None
        # Set by the backend: routine proposals (validate, apply on approval, revert).
        self.routines: Any = None
        self.integrations: Any = None  # integrations.Integrations, set by the backend
        # Set by the backend: notifications (kind, title, body, url, ref_kind, ref_id).
        self.notify: Callable[..., None] = lambda *a, **k: None
        live.getters.update(
            project=lambda i: self.project(i), thread=lambda i: self.thread(int(i)), message=lambda i: self.message(int(i)),
            question=lambda i: self.question(int(i)), permission=lambda i: self.permission(int(i)), task=lambda i: self.task(int(i)),
            claim=lambda i: self.claim(int(i)), suggestion=lambda i: self.suggestion(int(i)), promote=lambda i: self.promote(int(i)),
        )

    def _url(self, thread_id: int | None) -> str:
        return f"/t/{thread_id}" if thread_id else "/inbox"

    def _preview(self, text: str | None, n: int = 140) -> str:
        t = " ".join((text or "").split())
        return t if len(t) <= n else t[: n - 1] + "…"

    def r(self, text: str | None) -> str | None:
        return self.vault.redact(text)

    # ── idempotency ────────────────────────────────────────────────────────
    def once(self, key: str | None, endpoint: str, cls: type[BaseModel], make: Callable[[], BaseModel]) -> Any:
        """Repeat a create with the same key → the first result (the offline outbox retries).
        The key is claimed first (a placeholder row), so two concurrent retries can't both run `make`."""
        if not key:
            return make()
        now = self.db.now()
        try:
            self.db.ex("INSERT INTO idempotency_keys(key, endpoint, response_json, created_at) VALUES(?,?,?,?)", (key, endpoint, "", now))
            claimed = True
        except sqlite3.IntegrityError:   # the key exists
            claimed = False
        if not claimed:
            row = self.db.q1("SELECT endpoint, response_json, created_at FROM idempotency_keys WHERE key=?", (key,))
            if row is None:   # deleted in between (the first attempt failed): try again
                return self.once(key, endpoint, cls, make)
            if row["endpoint"] != endpoint:
                raise Conflict("idempotency_key_reused", "That key was used for a different request.")
            if row["response_json"]:
                return cls.model_validate_json(row["response_json"])
            # Still being made. An attempt that died leaves its claim behind: take it over after a minute.
            stale = iso(parse(now) - timedelta(seconds=60))  # type: ignore[operator]
            if row["created_at"] > stale or not self.db.ex("UPDATE idempotency_keys SET created_at=? WHERE key=? AND response_json='' AND created_at<=?", (now, key, stale)):
                raise Conflict("in_progress", "The first attempt of this request is still running; try again in a moment.")
        try:
            result = make()
        except BaseException:
            self.db.ex("DELETE FROM idempotency_keys WHERE key=? AND response_json=''", (key,))
            raise
        self.db.ex("UPDATE idempotency_keys SET response_json=? WHERE key=?", (result.model_dump_json(), key))
        return result

    # ── reading ────────────────────────────────────────────────────────────
    def _project_row(self, slug: str) -> dict:
        row = self.db.q1("SELECT * FROM projects WHERE slug=?", (slug,))
        if not row:
            raise NotFound(f"project {slug}")
        return row

    def _thread_row(self, thread_id: int) -> dict:
        row = self.db.q1("SELECT t.*, p.slug AS project FROM threads t LEFT JOIN projects p ON p.id=t.project_id WHERE t.id=?", (thread_id,))
        if not row:
            raise NotFound(f"thread {thread_id}")
        return row

    def _counts(self, thread_ids: list[int]) -> dict[int, dict]:
        """Unread, open asks and the last message for many threads, in one query per 500 threads."""
        out: dict[int, dict] = {}
        for i in range(0, len(thread_ids), 500):
            chunk = thread_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            asks = " + ".join(f"(SELECT COUNT(*) FROM {tb} x WHERE x.thread_id=t.id AND x.status='open')" for tb in ("questions", "permissions", "tasks"))
            rows = self.db.q(
                "SELECT t.id, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id AND m.sender='assistant' AND m.id>COALESCE(t.last_read_message_id,0)) AS unread, "
                f"{asks} AS open_asks, "
                "(SELECT sender FROM messages WHERE thread_id=t.id ORDER BY id DESC LIMIT 1) AS last_sender, "
                f"(SELECT body FROM messages WHERE thread_id=t.id ORDER BY id DESC LIMIT 1) AS last_body FROM threads t WHERE t.id IN ({marks})", chunk)
            out.update({r["id"]: r for r in rows})
        return out

    def thread_model(self, row: dict, counts: dict | None = None) -> s.Thread:
        c = counts if counts is not None else self._counts([row["id"]]).get(row["id"], {})
        preview = None
        if c.get("last_sender") is not None:
            who = {"owner": "You: ", "chat": "Chat: ", "system": ""}.get(c["last_sender"], "")
            preview = (who + " ".join(c["last_body"].split()))[:PREVIEW]
        return s.Thread(id=row["id"], project=row.get("project"), title=row["title"], type=row["type"], status=row["status"], created_by=row["created_by"],
                        pinned=bool(row["pinned"]), created_at=parse(row["created_at"]), last_activity_at=parse(row["updated_at"]),  # type: ignore[arg-type]
                        last_message_preview=preview, unread=c.get("unread", 0), open_asks=c.get("open_asks", 0))

    def thread(self, thread_id: int) -> s.Thread:
        return self.thread_model(self._thread_row(thread_id))

    def threads(self, project: str | None, general: bool, archived: bool) -> list[s.Thread]:
        sql = "SELECT t.*, p.slug AS project FROM threads t LEFT JOIN projects p ON p.id=t.project_id WHERE 1=1"
        params: list[Any] = []
        if not archived:
            sql += " AND t.status='open'"
        if general:
            sql += " AND t.project_id IS NULL"
        if project:
            sql += " AND p.slug=?"
            params.append(project)
        rows = self.db.q(sql + " ORDER BY t.pinned DESC, t.updated_at DESC", params)
        counts = self._counts([r["id"] for r in rows])
        return [self.thread_model(r, counts.get(r["id"], {})) for r in rows]

    def project_model(self, row: dict, threads: list[dict] | None = None, counts: dict | None = None) -> s.Project:
        if threads is None:
            threads = self.db.q("SELECT * FROM threads WHERE project_id=?", (row["id"],))
        if counts is None:
            counts = self._counts([t["id"] for t in threads])
        last = max([row["updated_at"], *[t["updated_at"] for t in threads]])
        return s.Project(slug=row["slug"], name=row["name"], summary=row["summary"], status=row["status"], doc_path=row["doc_path"],
                         links=_j(row["links_json"], []), created_by=row["created_by"], created_at=parse(row["created_at"]),  # type: ignore[arg-type]
                         last_activity_at=parse(last), unread=sum(counts.get(t["id"], {}).get("unread", 0) for t in threads if t["status"] == "open"),  # type: ignore[arg-type]
                         open_asks=sum(counts.get(t["id"], {}).get("open_asks", 0) for t in threads),
                         thread_count=sum(1 for t in threads if t["status"] == "open"))

    def project(self, slug: str) -> s.Project:
        return self.project_model(self._project_row(slug))

    def projects(self, status: str | None) -> list[s.Project]:
        rows = self.db.q("SELECT * FROM projects WHERE (? IS NULL OR status=?)", (status, status))
        by_project: dict[int, list[dict]] = {r["id"]: [] for r in rows}
        for th in self.db.q("SELECT * FROM threads WHERE project_id IS NOT NULL"):
            if th["project_id"] in by_project:
                by_project[th["project_id"]].append(th)
        counts = self._counts([th["id"] for ths in by_project.values() for th in ths])
        return sorted((self.project_model(r, by_project[r["id"]], counts) for r in rows), key=lambda p: p.last_activity_at, reverse=True)

    def message_model(self, row: dict) -> s.Message:
        atts = [s.Attachment(name=a["name"], volume=a["volume"], path=a["path"], size=a["size"], mime=a["mime"])
                for a in self.db.q("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (row["id"],))]
        return s.Message(id=row["id"], thread_id=row["thread_id"], sender=row["sender"], body=row["body"], via=row["via"], run_id=row["run_id"],
                         attachments=atts, delivery=row["delivery"], created_at=parse(row["created_at"]))  # type: ignore[arg-type]

    def message(self, mid: int) -> s.Message:
        row = self.db.q1("SELECT * FROM messages WHERE id=?", (mid,))
        if not row:
            raise NotFound(f"message {mid}")
        return self.message_model(row)

    def _get(self, table: str, id_: int, what: str) -> dict:
        row = self.db.q1(f"SELECT * FROM {table} WHERE id=?", (id_,))
        if not row:
            raise NotFound(f"{what} {id_}")
        return row

    def question_model(self, r: dict) -> s.Question:
        return s.Question(id=r["id"], thread_id=r["thread_id"], run_id=r["run_id"], kind=r["kind"], text=r["text"], options=_j(r["options_json"], []),
                          default=_j(r["default_json"]), scale_labels=_j(r["scale_labels_json"]), status=r["status"], answer=_j(r["answer_json"]),
                          note=r["note"], created_at=parse(r["created_at"]), answered_at=parse(r["answered_at"]))  # type: ignore[arg-type]

    def question(self, qid: int) -> s.Question:
        return self.question_model(self._get("questions", qid, "question"))

    def _scopes(self, r: dict) -> list:
        if self.db.q1("SELECT 1 FROM permission_actions WHERE permission_id=? AND kind='mcp_call'", (r["id"],)):
            return ["once", "always"]  # this exact call once, or the tool always
        has_project = r["thread_id"] and self.db.q1("SELECT project_id FROM threads WHERE id=?", (r["thread_id"],))["project_id"]
        return ["once", "project", "always"] if has_project else ["once", "always"]

    def permission_model(self, r: dict) -> s.Permission:
        actions = [s.PermissionAction(kind=a["kind"], summary=_action_summary(a["kind"], _j(a["payload_json"], {})), payload=_j(a["payload_json"], {}))
                   for a in self.db.q("SELECT * FROM permission_actions WHERE permission_id=? ORDER BY id", (r["id"],))]
        return s.Permission(id=r["id"], thread_id=r["thread_id"], run_id=r["run_id"], text=r["text"], reason=r["reason"], actions=actions,
                            scopes=self._scopes(r), status=r["status"], scope=r["scope"], note=r["note"],  # type: ignore[arg-type]
                            created_at=parse(r["created_at"]), decided_at=parse(r["decided_at"]))  # type: ignore[arg-type]

    def permission(self, pid: int) -> s.Permission:
        return self.permission_model(self._get("permissions", pid, "permission"))

    def task_model(self, r: dict) -> s.Task:
        return s.Task(id=r["id"], thread_id=r["thread_id"], run_id=r["run_id"], text=r["text"], detail=r["detail"], status=r["status"], note=r["note"],
                      created_at=parse(r["created_at"]), resolved_at=parse(r["resolved_at"]))  # type: ignore[arg-type]

    def task(self, tid: int) -> s.Task:
        return self.task_model(self._get("tasks", tid, "task"))

    def claim_model(self, r: dict) -> s.Claim:
        title = (self.db.q1("SELECT title FROM threads WHERE id=?", (r["thread_id"],)) or {}).get("title", "")
        return s.Claim(id=r["id"], run_id=r["run_id"], thread_id=r["thread_id"], thread_title=title, plan=r["plan"], status=r["status"], result=r["result"],
                       created_at=parse(r["created_at"]), updated_at=parse(r["updated_at"]))  # type: ignore[arg-type]

    def claim(self, cid: int) -> s.Claim:
        return self.claim_model(self._get("claims", cid, "claim"))

    def claims(self, active: bool, run_id: int | None = None) -> list[s.Claim]:
        sql, params = "SELECT * FROM claims WHERE 1=1", []
        if active:
            sql += " AND status IN ('planned','in_progress')"
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        return [self.claim_model(r) for r in self.db.q(sql + " ORDER BY id", params)]

    def suggestion_model(self, r: dict) -> s.Suggestion:
        return s.Suggestion(id=r["id"], run_id=r["run_id"], kind=r["kind"], target_kind=r["target_kind"], target_id=r["target_id"], text=r["text"],
                            payload=_j(r["payload_json"], {}), status=r["status"], created_at=parse(r["created_at"]), decided_at=parse(r["decided_at"]))  # type: ignore[arg-type]

    def suggestion(self, sid: int) -> s.Suggestion:
        return self.suggestion_model(self._get("suggestions", sid, "suggestion"))

    def promote_model(self, r: dict) -> s.PromoteRequest:
        return s.PromoteRequest(id=r["id"], run_id=r["run_id"], thread_id=r["thread_id"], reason=r["reason"], budget=r["budget"], status=r["status"],
                                note=r["note"], created_at=parse(r["created_at"]), decided_at=parse(r["decided_at"]))  # type: ignore[arg-type]

    def promote(self, pid: int) -> s.PromoteRequest:
        return self.promote_model(self._get("promote_requests", pid, "promote request"))

    def thread_items(self, thread_id: int, before: str | None, limit: int) -> s.ThreadItems:
        self._thread_row(thread_id)
        live = "AND status!='withdrawn'"
        # (type, table, where, order within a timestamp, model, wrapper); the page is cut in SQL, then rows are loaded by id.
        sources: list[tuple[str, str, str, int, Callable, Callable]] = [
            ("message", "messages", "thread_id=:t", 0, self.message_model, lambda x: s.MessageItem(at=x.created_at, message=x)),
            ("claim", "claims", "thread_id=:t", 1, self.claim_model, lambda x: s.ClaimItem(at=x.created_at, claim=x)),
            ("question", "questions", f"thread_id=:t {live}", 2, self.question_model, lambda x: s.QuestionItem(at=x.created_at, question=x)),
            ("permission", "permissions", f"thread_id=:t {live}", 3, self.permission_model, lambda x: s.PermissionItem(at=x.created_at, permission=x)),
            ("task", "tasks", f"thread_id=:t {live}", 4, self.task_model, lambda x: s.TaskItem(at=x.created_at, task=x)),
            ("suggestion", "suggestions", f"target_kind='thread' AND target_id=:t {live}", 5, self.suggestion_model, lambda x: s.SuggestionItem(at=x.created_at, suggestion=x)),
            ("promote", "promote_requests", "thread_id=:t", 6, self.promote_model, lambda x: s.PromoteItem(at=x.created_at, promote=x)),
        ]
        cut = " AND created_at < :before" if before else ""
        union = " UNION ALL ".join(f"SELECT '{ty}' AS ty, id, created_at, {o} AS o FROM {table} WHERE {where}{cut}" for ty, table, where, o, _, _ in sources)
        params: dict[str, Any] = {"t": thread_id, "n": limit + 1}
        if before:
            params["before"] = iso(parse(before))  # type: ignore[arg-type]
        page = self.db.q(f"SELECT ty, id FROM ({union}) ORDER BY created_at DESC, o DESC, id DESC LIMIT :n", params)  # type: ignore[arg-type]
        older = len(page) > limit
        page = page[:limit][::-1]
        by_type = {ty: (table, model, wrap) for ty, table, _, _, model, wrap in sources}
        loaded: dict[tuple[str, int], Any] = {}
        for ty, (table, model, wrap) in by_type.items():
            ids = [r["id"] for r in page if r["ty"] == ty]
            if ids:
                marks = ",".join("?" * len(ids))
                loaded.update({(ty, r["id"]): wrap(model(r)) for r in self.db.q(f"SELECT * FROM {table} WHERE id IN ({marks})", ids)})
        items: list[Any] = [loaded[(r["ty"], r["id"])] for r in page]
        return s.ThreadItems(items=items, before_cursor=items[0].at.isoformat() if older and items else None)

    def recent_messages(self, sender: str | None, limit: int) -> list[s.RecentMessage]:
        rows = self.db.q("SELECT m.*, t.title AS thread_title, p.slug AS project FROM messages m JOIN threads t ON t.id=m.thread_id "
                         "LEFT JOIN projects p ON p.id=t.project_id WHERE (? IS NULL OR m.sender=?) ORDER BY m.id DESC LIMIT ?", (sender, sender, limit))
        return [s.RecentMessage(message=self.message_model(r), thread_title=r["thread_title"], project=r["project"]) for r in rows]

    # ── writing: projects and threads ──────────────────────────────────────
    def _touch_thread(self, thread_id: int) -> None:
        now = self.db.now()
        self.db.ex("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
        pid = self.db.q1("SELECT project_id FROM threads WHERE id=?", (thread_id,))["project_id"]
        self.live.changed("thread", thread_id)
        if pid:
            slug = self.db.q1("SELECT slug FROM projects WHERE id=?", (pid,))["slug"]
            self.live.changed("project", slug)

    def create_project(self, name: str, slug: str | None, summary: str, by: str, doc_path: str | None = None, links: list[dict] | None = None) -> s.Project:
        base = slug or slugify(name)
        if not SLUG_RE.match(base):
            raise BadRequest("bad_slug", "Slugs are lowercase letters, digits and dashes.")
        candidate, n = base, 2
        while self.db.q1("SELECT 1 FROM projects WHERE slug=?", (candidate,)):
            if slug:
                raise Conflict("slug_taken", f"A project called {slug} exists.")
            candidate, n = f"{base}-{n}", n + 1
        now = self.db.now()
        self.db.ex("INSERT INTO projects(slug, name, summary, doc_path, links_json, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (candidate, self.r(name.strip()), self.r(summary.strip()), doc_path, json.dumps(links or []), by, now, now))
        self.live.changed("project", candidate)
        return self.project(candidate)

    def patch_project(self, slug: str, fields: dict) -> s.Project:
        row = self._project_row(slug)
        allowed = {k: v for k, v in fields.items() if k in ("name", "summary", "status", "doc_path", "links") and v is not None}
        if "links" in allowed:
            allowed["links_json"] = json.dumps(allowed.pop("links"))
        if not allowed:
            return self.project(slug)
        sets = ", ".join(f"{k}=?" for k in allowed)
        self.db.ex(f"UPDATE projects SET {sets}, updated_at=? WHERE id=?", (*[self.r(v) if isinstance(v, str) else v for v in allowed.values()], self.db.now(), row["id"]))
        self.live.changed("project", slug)
        return self.project(slug)

    def create_thread(self, title: str, project: str | None, by: str, first_message: str | None = None, run_id: int | None = None,
                      upload_ids: list[int] | None = None) -> s.Thread:
        pid = self._project_row(project)["id"] if project else None
        now = self.db.now()
        tid = self.db.ex("INSERT INTO threads(project_id, title, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
                         (pid, self.r(title.strip())[:200], by, now, now))  # type: ignore[index]
        if by == "owner":  # the owner has read their own new thread
            self.db.ex("UPDATE threads SET last_read_message_id=(SELECT COALESCE(MAX(id),0) FROM messages) WHERE id=?", (tid,))
        self._touch_thread(tid)
        if first_message:
            if by == "owner":
                self.post_owner_message(tid, first_message, upload_ids or [], None)
            else:
                self.post_assistant_message(tid, first_message, run_id)
        return self.thread(tid)

    def patch_thread(self, thread_id: int, fields: dict) -> s.Thread:
        row = self._thread_row(thread_id)
        sets: dict[str, Any] = {}
        if fields.get("title"):
            sets["title"] = self.r(fields["title"].strip())[:200]  # type: ignore[index]
        if "project" in fields and fields["project"] is not None:
            sets["project_id"] = self._project_row(fields["project"])["id"] if fields["project"] else None
        if fields.get("status"):
            sets["status"] = fields["status"]
            sets["archived_at"] = self.db.now() if fields["status"] == "archived" else None
        if fields.get("pinned") is not None:
            sets["pinned"] = int(fields["pinned"])
        if sets:
            self.db.ex(f"UPDATE threads SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), thread_id))
            self._touch_thread(thread_id)
            if row["project"] and "project_id" in sets:
                self.live.changed("project", row["project"])
        return self.thread(thread_id)

    def mark_read(self, thread_id: int, message_id: int | None) -> None:
        self._thread_row(thread_id)
        last = message_id or self.db.q1("SELECT COALESCE(MAX(id),0) AS n FROM messages WHERE thread_id=?", (thread_id,))["n"]
        if self.db.ex("UPDATE threads SET last_read_message_id=? WHERE id=? AND COALESCE(last_read_message_id,0)<?", (last, thread_id, last)):
            self.live.changed("thread", thread_id)
            self.live.emit("unread", {"thread_id": thread_id})

    # ── writing: messages ──────────────────────────────────────────────────
    def post_owner_message(self, thread_id: int, body: str, upload_ids: list[int], key: str | None, via: str = "web", sender: str = "owner") -> s.Message:
        def make() -> s.Message:
            t = self._thread_row(thread_id)
            if t["status"] == "archived":
                self.patch_thread(thread_id, {"status": "open"})
            mid = self.db.ex("INSERT INTO messages(thread_id, sender, body, via, created_at) VALUES(?,?,?,?,?)",
                             (thread_id, sender, self.r(body), via, self.db.now()))
            for uid in upload_ids:
                u = self.db.q1("SELECT * FROM uploads WHERE id=?", (uid,))
                if u:
                    self.db.ex("INSERT INTO attachments(message_id, upload_id, name, volume, path, size, mime) VALUES(?,?,?,?,?,?,?)",
                               (mid, uid, u["name"], "work", u["path"], u["size"], u["mime"]))
            self.db.ex("UPDATE threads SET last_read_message_id=? WHERE id=?", (mid, thread_id))
            self.live.changed("message", mid)
            self._touch_thread(thread_id)
            self.fire("message_posted", {"message_id": mid, "thread_id": thread_id, "sender": sender, "body": self.r(body)})
            return self.message(mid)

        return self.once(key, f"message:{thread_id}", s.Message, make)

    def post_assistant_message(self, thread_id: int, body: str, run_id: int | None) -> s.Message:
        t = self._thread_row(thread_id)
        if t["status"] == "archived":
            raise Conflict("archived", f"Thread {thread_id} is archived; start a new thread or suggest reopening it.")
        mid = self.db.ex("INSERT INTO messages(thread_id, sender, body, run_id, delivery, created_at) VALUES(?,?,?,?,'delivered',?)",
                         (thread_id, "assistant", self.r(body), run_id, self.db.now()))
        self.live.changed("message", mid)
        self._touch_thread(thread_id)
        self.fire("message_posted", {"message_id": mid, "thread_id": thread_id, "sender": "assistant", "body": self.r(body)})
        self.notify("assistant_message", f"The assistant in “{t['title']}”", self._preview(self.r(body)), self._url(thread_id), "message", str(mid))
        return self.message(mid)

    def system_message(self, thread_id: int, body: str) -> None:
        mid = self.db.ex("INSERT INTO messages(thread_id, sender, body, delivery, delivered_at, created_at) VALUES(?,?,?,'delivered',?,?)",
                         (thread_id, "system", body, self.db.now(), self.db.now()))
        self.live.changed("message", mid)
        self._touch_thread(thread_id)

    # ── writing: asks (filed by the agent, decided by the owner) ───────────
    def _ask_thread(self, thread_id: int | None) -> int | None:
        if thread_id is not None:
            self._thread_row(thread_id)
        return thread_id

    def _card(self, entity: str, id_: int, thread_id: int | None) -> None:
        self.live.changed(entity, id_)
        self.live.emit("inbox_counts")
        if thread_id:
            self._touch_thread(thread_id)

    def _check_ask_cap(self, run_id: int | None) -> None:
        """A run can't have more than MAX_OPEN_ASKS_PER_RUN asks open at once (a loop would bury the owner)."""
        if run_id is None:
            return
        n = sum(self.db.q1(f"SELECT COUNT(*) AS n FROM {t} WHERE run_id=? AND status='open'", (run_id,))["n"] for t in ("questions", "permissions", "tasks"))
        if n >= self.settings.get("MAX_OPEN_ASKS_PER_RUN"):
            raise BadRequest("too_many_asks", f"This run already has {n} asks waiting for the owner. Wait for answers, or withdraw some, before asking more.")

    def file_question(self, run_id: int | None, thread_id: int | None, kind: str, text: str, options: list[str], default: Any, scale_labels: list[str] | None) -> s.Question:
        self._check_ask_cap(run_id)
        if kind in ("single", "multi", "slot") and len(options) < 2:
            raise BadRequest("options", f"A {kind} question needs at least two options.")
        if kind == "scale" and scale_labels is not None and len(scale_labels) != 2:
            raise BadRequest("scale_labels", "Give two scale labels: low and high.")
        if default is None:
            raise BadRequest("default", "Say what you'll assume if there's no answer (--default).")
        qid = self.db.ex("INSERT INTO questions(thread_id, run_id, kind, text, options_json, default_json, scale_labels_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (self._ask_thread(thread_id), run_id, kind, self.r(text), json.dumps([self.r(o) for o in options]), json.dumps(default),
                          json.dumps(scale_labels) if scale_labels else None, self.db.now()))
        self._card("question", qid, thread_id)
        q = self.question(qid)
        self.notify("question", "The assistant asks", self._preview(q.text), self._url(q.thread_id), "question", str(qid))
        return q

    def file_permission(self, run_id: int | None, thread_id: int | None, text: str, reason: str | None, actions: list[dict],
                        trusted: bool = False) -> s.Permission:
        """`trusted`: filed by control itself (a brokered call), so 'mcp_call' actions are accepted."""
        self._check_ask_cap(run_id)
        checked = [self._check_routine(a) if a.get("kind") == "routine"
                   else self.integrations.check_action(a) if trusted and a.get("kind") == "mcp_call" and self.integrations
                   else _check_action(a) for a in actions]
        with self.db.tx() as db:
            pid = db.ex("INSERT INTO permissions(thread_id, run_id, text, reason, created_at) VALUES(?,?,?,?,?)",
                        (self._ask_thread(thread_id), run_id, self.r(text), self.r(reason), db.now()))
            for kind, payload in checked:
                db.ex("INSERT INTO permission_actions(permission_id, kind, payload_json) VALUES(?,?,?)", (pid, kind, json.dumps(payload)))
        self._card("permission", pid, thread_id)
        p = self.permission(pid)
        title = "Secure input needed" if any(a.kind == "secret" for a in p.actions) else "Approval needed"
        self.notify("approval", title, self._preview(p.text), self._url(p.thread_id), "permission", str(pid))
        return p

    def _check_routine(self, a: dict) -> tuple[str, dict]:
        if self.routines is None:
            raise BadRequest("action", "Routines aren't available.")
        p = a.get("payload") if isinstance(a.get("payload"), dict) else a
        try:
            req = s.RoutineCreate(name=p.get("name", ""), prompt=p.get("prompt", ""), schedule=p.get("schedule") or {}, tier=p.get("tier", "sonnet/medium"),
                                  budget=float(p.get("budget", 0)), provider=p.get("provider"))
        except (ValueError, TypeError) as e:
            raise BadRequest("action", f"Bad routine: {e}") from None
        return "routine", self.routines.propose_payload(req)

    def file_task(self, run_id: int | None, thread_id: int | None, text: str, detail: str | None) -> s.Task:
        self._check_ask_cap(run_id)
        tid = self.db.ex("INSERT INTO tasks(thread_id, run_id, text, detail, created_at) VALUES(?,?,?,?,?)",
                         (self._ask_thread(thread_id), run_id, self.r(text), self.r(detail), self.db.now()))
        self._card("task", tid, thread_id)
        t = self.task(tid)
        self.notify("task", "A task for you", self._preview(t.text), self._url(t.thread_id), "task", str(tid))
        return t

    def withdraw(self, kind: str, id_: int) -> None:
        table = {"question": "questions", "permission": "permissions", "task": "tasks", "suggestion": "suggestions"}[kind]
        row = self._get(table, id_, kind)
        if row["status"] != "open":
            raise Conflict("not_open", f"{kind} {id_} is {row['status']}")
        self.db.ex(f"UPDATE {table} SET status='withdrawn' WHERE id=?", (id_,))
        self._card(kind, id_, row.get("thread_id") if kind != "suggestion" else None)

    def _open_row(self, table: str, id_: int, what: str) -> dict:
        row = self._get(table, id_, what)
        if row["status"] != "open":
            raise Conflict("already_decided", f"{what} {id_} is {row['status']}")
        return row

    def answer_question(self, qid: int, req: s.QuestionAnswer) -> s.Question:
        def make() -> s.Question:
            x = self.question(qid)
            if x.status != "open":
                raise Conflict("already_decided", f"question {qid} is {x.status}")
            ok = {"yes_no": lambda v: isinstance(v, bool), "multi": lambda v: isinstance(v, list) and all(o in x.options for o in v),
                  "single": lambda v: v in x.options, "slot": lambda v: v in x.options, "text": lambda v: isinstance(v, str) and v.strip() != "",
                  "scale": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5}[x.kind]
            if not ok(req.value):
                raise BadRequest("invalid_answer", f"Not a valid answer for a {x.kind} question.")
            value = self.r(req.value) if isinstance(req.value, str) else req.value
            self.db.ex("UPDATE questions SET status='answered', answer_json=?, note=?, answered_at=? WHERE id=?",
                       (json.dumps(value), self.r(req.note), self.db.now(), qid))
            self._card("question", qid, x.thread_id)
            self.fire("ask_decided", {"kind": "question", "id": qid, "decision": value})
            return self.question(qid)

        return self.once(req.idempotency_key, f"answer:{qid}", s.Question, make)

    def decide_permission(self, pid: int, req: s.PermissionDecision) -> s.Permission:
        row = self._open_row("permissions", pid, "permission")
        p = self.permission_model(row)
        if req.decision == "allow":
            if req.scope not in p.scopes:
                raise BadRequest("invalid_scope", f"Choose one of {', '.join(p.scopes)}.")
            for a in self.db.q("SELECT * FROM permission_actions WHERE permission_id=? AND kind='secret'", (pid,)):
                name = _j(a["payload_json"], {})["name"]
                if not self.vault.exists(name):
                    raise Conflict("secret_missing", f"Enter the value for {name} first.")
            for a in self.db.q("SELECT * FROM permission_actions WHERE permission_id=? AND applied_at IS NULL ORDER BY id", (pid,)):
                self._apply(a, req.scope)
        self.db.ex("UPDATE permissions SET status=?, scope=?, note=?, decided_at=? WHERE id=?",
                   ("allowed" if req.decision == "allow" else "denied", req.scope if req.decision == "allow" else None, self.r(req.note), self.db.now(), pid))
        self._card("permission", pid, p.thread_id)
        self.fire("ask_decided", {"kind": "permission", "id": pid, "decision": req.decision})
        return self.permission(pid)

    def _apply(self, a: dict, scope: str | None = None) -> None:
        """Apply one approved action. Control does this, never the agent."""
        payload = _j(a["payload_json"], {})
        result: dict[str, Any] = {}
        try:
            if a["kind"] == "mcp_call":
                if scope == "always" and self.integrations:
                    self.integrations.apply_always(payload)
                return  # a one-time approval is used by the call itself (integrations._grant)
            if a["kind"] == "egress_rule":
                if self.add_egress_rule is None:
                    raise RuntimeError("egress isn't configured")
                result["rule_id"] = self.add_egress_rule(payload["pattern"], payload.get("mode", "allow_ro"), f"permission #{a['permission_id']}")
            elif a["kind"] == "routine":
                result = self.routines.apply(payload, (self.db.q1("SELECT run_id FROM permissions WHERE id=?", (a["permission_id"],)) or {}).get("run_id"))
            elif a["kind"] == "setting":
                old = self.db.q1("SELECT value_json FROM settings WHERE key=?", (payload["key"],))
                result["previous"] = old["value_json"] if old else None
                self.settings.set(payload["key"], payload["value"])
        except Exception as e:  # noqa: BLE001
            self.db.ex("UPDATE permission_actions SET error=? WHERE id=?", (str(e)[:500], a["id"]))
            raise Conflict("action_failed", f"Couldn't apply '{_action_summary(a['kind'], payload)}': {e}") from e
        self.db.ex("UPDATE permission_actions SET applied_at=?, result_json=?, error=NULL WHERE id=?", (self.db.now(), json.dumps(result), a["id"]))

    def revert_once(self) -> list[str]:
        """At the end of a run: undo every applied 'once' action (only one run exists at a time, so an approval
        given after the asking run ended is undone at the end of the next run)."""
        rows = self.db.q(
            "SELECT a.*, p.run_id AS p_run FROM permission_actions a JOIN permissions p ON p.id=a.permission_id "
            "WHERE p.status='allowed' AND p.scope='once' AND a.applied_at IS NOT NULL AND a.reverted_at IS NULL")
        undone = []
        for a in rows:
            payload, result = _j(a["payload_json"], {}), _j(a["result_json"], {})
            if a["kind"] == "mcp_call":
                continue
            try:
                if a["kind"] == "egress_rule" and result.get("rule_id") and self.delete_egress_rule:
                    self.delete_egress_rule(int(result["rule_id"]))
                elif a["kind"] == "setting":
                    if result.get("previous") is None:
                        self.db.ex("DELETE FROM settings WHERE key=?", (payload["key"],))
                    else:
                        self.settings.set(payload["key"], json.loads(result["previous"]))
                elif a["kind"] == "secret" and self.vault.exists(payload["name"]):
                    self.vault.delete(payload["name"])
                elif a["kind"] == "routine" and self.routines:
                    self.routines.revert(result)
            except Exception as e:  # noqa: BLE001
                self.db.ex("UPDATE permission_actions SET error=? WHERE id=?", (f"revert failed: {e}"[:500], a["id"]))
                continue
            self.db.ex("UPDATE permission_actions SET reverted_at=? WHERE id=?", (self.db.now(), a["id"]))
            undone.append(_action_summary(a["kind"], payload))
        return undone

    def resolve_task(self, tid: int, req: s.TaskResolve) -> s.Task:
        def make() -> s.Task:
            row = self._open_row("tasks", tid, "task")
            self.db.ex("UPDATE tasks SET status=?, note=?, resolved_at=? WHERE id=?", (req.outcome, self.r(req.note), self.db.now(), tid))
            self._card("task", tid, row["thread_id"])
            self.fire("ask_decided", {"kind": "task", "id": tid, "decision": req.outcome})
            return self.task(tid)

        return self.once(req.idempotency_key, f"task:{tid}", s.Task, make)

    def file_promote(self, run_id: int, thread_id: int | None, reason: str, budget: float) -> s.PromoteRequest:
        if self.db.q1("SELECT 1 FROM promote_requests WHERE run_id=? AND status='open'", (run_id,)):
            raise Conflict("already_open", "This run already has a promote request waiting for the owner.")
        pid = self.db.ex("INSERT INTO promote_requests(run_id, thread_id, reason, budget, created_at) VALUES(?,?,?,?,?)",
                         (run_id, thread_id, self.r(reason), budget, self.db.now()))
        self._card("promote", pid, thread_id)
        self.notify("approval", f"Promote a reply to a full run (+{budget:g} pts)?", self._preview(self.r(reason)), self._url(thread_id), "promote", str(pid))
        return self.promote(pid)

    def expire_promotes(self, run_id: int | None = None, older_than: str | None = None) -> None:
        rows = self.db.q("SELECT id, thread_id FROM promote_requests WHERE status='open' AND (? IS NULL OR run_id=?) AND (? IS NULL OR created_at<?)",
                         (run_id, run_id, older_than, older_than))
        for r in rows:
            self.db.ex("UPDATE promote_requests SET status='expired', decided_at=? WHERE id=?", (self.db.now(), r["id"]))
            self._card("promote", r["id"], r["thread_id"])

    def decide_promote(self, pid: int, req: s.PromoteDecision) -> s.PromoteRequest:
        row = self._open_row("promote_requests", pid, "promote request")
        self.db.ex("UPDATE promote_requests SET status=?, note=?, decided_at=? WHERE id=?",
                   ("approved" if req.decision == "approve" else "denied", self.r(req.note), self.db.now(), pid))
        self._card("promote", pid, row["thread_id"])
        self.fire("ask_decided", {"kind": "promote", "id": pid, "decision": req.decision})
        return self.promote(pid)

    # ── claims ─────────────────────────────────────────────────────────────
    def claim_thread(self, run_id: int, thread_id: int, plan: str) -> s.Claim:
        self._thread_row(thread_id)
        existing = self.db.q1("SELECT id FROM claims WHERE run_id=? AND thread_id=? AND status IN ('planned','in_progress')", (run_id, thread_id))
        now = self.db.now()
        if existing:
            self.db.ex("UPDATE claims SET plan=?, updated_at=? WHERE id=?", (self.r(plan), now, existing["id"]))
            cid = existing["id"]
        else:
            cid = self.db.ex("INSERT INTO claims(run_id, thread_id, plan, status, created_at, updated_at) VALUES(?,?,?,'in_progress',?,?)",
                             (run_id, thread_id, self.r(plan), now, now))
        self.live.changed("claim", cid)
        self._touch_thread(thread_id)
        return self.claim(cid)

    def close_claim(self, run_id: int, thread_id: int, status: str, result: str) -> s.Claim:
        row = self.db.q1("SELECT id FROM claims WHERE run_id=? AND thread_id=? AND status IN ('planned','in_progress') ORDER BY id DESC LIMIT 1", (run_id, thread_id))
        if not row:
            raise NotFound(f"no open claim on thread {thread_id} in this run")
        self.db.ex("UPDATE claims SET status=?, result=?, updated_at=? WHERE id=?", (status, self.r(result), self.db.now(), row["id"]))
        self.live.changed("claim", row["id"])
        self._touch_thread(thread_id)
        return self.claim(row["id"])

    def drop_open_claims(self, run_id: int) -> None:
        """A run that ends with claims still open: mark them partial."""
        for row in self.db.q("SELECT id, thread_id FROM claims WHERE run_id=? AND status IN ('planned','in_progress')", (run_id,)):
            self.db.ex("UPDATE claims SET status='partial', result=COALESCE(result, 'The run ended before this was closed.'), updated_at=? WHERE id=?",
                       (self.db.now(), row["id"]))
            self.live.changed("claim", row["id"])

    # ── suggestions ────────────────────────────────────────────────────────
    def suggest(self, run_id: int | None, kind: str, target: str, text: str, payload: dict) -> s.Suggestion:
        if kind not in SUGGESTION_KINDS:
            raise BadRequest("kind", f"Unknown suggestion kind {kind}.")
        target_kind = SUGGESTION_KINDS[kind]
        if target_kind == "thread":
            target_id = self._thread_row(int(target))["id"]
        else:
            target_id = self._project_row(target)["id"]
        payload = self._check_suggestion(kind, target_id, payload)
        sid = self.db.ex("INSERT INTO suggestions(run_id, kind, target_kind, target_id, text, payload_json, created_at) VALUES(?,?,?,?,?,?,?)",
                         (run_id, kind, target_kind, target_id, self.r(text), json.dumps(payload), self.db.now()))
        self._card("suggestion", sid, target_id if target_kind == "thread" else None)
        return self.suggestion(sid)

    def _check_suggestion(self, kind: str, target_id: int, p: dict) -> dict:
        if kind == "thread_rename":
            if not str(p.get("title", "")).strip():
                raise BadRequest("payload", "thread_rename needs a title.")
            return {"title": str(p["title"]).strip()[:200]}
        if kind == "thread_move":
            project = p.get("project") or ""
            if project:
                self._project_row(project)
            return {"project": project}
        if kind == "thread_merge":
            into = int(p.get("into", 0))
            if into == target_id:
                raise BadRequest("payload", "Can't merge a thread into itself.")
            self._thread_row(into)
            return {"into": into}
        if kind == "thread_split":
            ids = [int(i) for i in p.get("message_ids", [])]
            if not ids or not str(p.get("title", "")).strip():
                raise BadRequest("payload", "thread_split needs a title and message_ids.")
            n = self.db.q1(f"SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND id IN ({','.join('?' * len(ids))})", (target_id, *ids))["n"]
            if n != len(ids):
                raise BadRequest("payload", "Every message to split off must be in the thread.")
            return {"title": str(p["title"]).strip()[:200], "message_ids": ids}
        if kind == "project_merge":
            into = str(p.get("into", ""))
            if self._project_row(into)["id"] == target_id:
                raise BadRequest("payload", "Can't merge a project into itself.")
            return {"into": into}
        if kind == "project_split":
            ids = [int(i) for i in p.get("thread_ids", [])]
            if not ids or not str(p.get("name", "")).strip():
                raise BadRequest("payload", "project_split needs a name and thread_ids.")
            n = self.db.q1(f"SELECT COUNT(*) AS n FROM threads WHERE project_id=? AND id IN ({','.join('?' * len(ids))})", (target_id, *ids))["n"]
            if n != len(ids):
                raise BadRequest("payload", "Every thread to split off must be in the project.")
            return {"name": str(p["name"]).strip()[:120], "thread_ids": ids}
        return {}

    def decide_suggestion(self, sid: int, req: s.SuggestionDecision) -> s.Suggestion:
        row = self._open_row("suggestions", sid, "suggestion")
        if req.decision == "accept":
            self._carry_out(row)
        self.db.ex("UPDATE suggestions SET status=?, decided_at=? WHERE id=?", ("accepted" if req.decision == "accept" else "dismissed", self.db.now(), sid))
        self._card("suggestion", sid, row["target_id"] if row["target_kind"] == "thread" else None)
        self.fire("ask_decided", {"kind": "suggestion", "id": sid, "decision": req.decision})
        return self.suggestion(sid)

    def accept_all(self) -> list[s.Suggestion]:
        out = []
        for row in self.db.q("SELECT id FROM suggestions WHERE status='open' ORDER BY id"):
            try:
                out.append(self.decide_suggestion(row["id"], s.SuggestionDecision(decision="accept")))
            except (NotFound, Conflict, BadRequest):
                continue  # e.g. its target was merged away by an earlier one
        return out

    def _carry_out(self, row: dict) -> None:
        kind, tid, p = row["kind"], row["target_id"], _j(row["payload_json"], {})
        if kind == "thread_archive":
            self.patch_thread(tid, {"status": "archived"})
        elif kind == "thread_rename":
            self.patch_thread(tid, {"title": p["title"]})
        elif kind == "thread_move":
            self.patch_thread(tid, {"project": p["project"]})
            self.system_message(tid, f"Moved to {p['project'] or 'General'}.")
        elif kind == "thread_merge":
            src, dst = self._thread_row(tid), self._thread_row(p["into"])
            with self.db.tx() as db:
                for table in ("messages", "questions", "permissions", "tasks", "claims", "promote_requests"):
                    db.ex(f"UPDATE {table} SET thread_id=? WHERE thread_id=?", (dst["id"], tid))
            self.system_message(dst["id"], f"Moved here from “{src['title']}” (l0wagent://thread/{tid}).")
            self.system_message(tid, f"Merged into “{dst['title']}” (l0wagent://thread/{dst['id']}).")
            self.patch_thread(tid, {"status": "archived"})
        elif kind == "thread_split":
            src = self._thread_row(tid)
            new = self.create_thread(p["title"], src["project"], "assistant")
            ids = p["message_ids"]
            self.db.ex(f"UPDATE messages SET thread_id=? WHERE thread_id=? AND id IN ({','.join('?' * len(ids))})", (new.id, tid, *ids))
            self.system_message(new.id, f"Split off from “{src['title']}” (l0wagent://thread/{tid}).")
            self.system_message(tid, f"{len(ids)} message(s) moved to “{new.title}” (l0wagent://thread/{new.id}).")
        elif kind in ("project_stale", "project_archive"):
            slug = self.db.q1("SELECT slug FROM projects WHERE id=?", (tid,))["slug"]
            self.patch_project(slug, {"status": "stale" if kind == "project_stale" else "archived"})
        elif kind == "project_merge":
            src = self.db.q1("SELECT * FROM projects WHERE id=?", (tid,))
            for t in self.db.q("SELECT id FROM threads WHERE project_id=?", (tid,)):
                self.patch_thread(t["id"], {"project": p["into"]})
                self.system_message(t["id"], f"Moved from {src['name']} (merged into {p['into']}).")  # type: ignore[index]
            self.patch_project(src["slug"], {"status": "archived"})  # type: ignore[index]
        elif kind == "project_split":
            new = self.create_project(p["name"], None, "", "assistant")
            for t in p["thread_ids"]:
                self.patch_thread(t, {"project": new.slug})
                self.system_message(t, f"Moved to the new project {new.name}.")

    # ── inbox ──────────────────────────────────────────────────────────────
    def _entry(self, kind: str, obj: Any) -> s.InboxEntry:
        tid = getattr(obj, "thread_id", None)
        if kind == "suggestion" and obj.target_kind == "thread":
            tid = obj.target_id
        t = self.db.q1("SELECT t.title, p.slug FROM threads t LEFT JOIN projects p ON p.id=t.project_id WHERE t.id=?", (tid,)) if tid else None
        return s.InboxEntry(kind=kind, id=obj.id, at=obj.created_at, thread_id=tid, thread_title=t["title"] if t else None,  # type: ignore[arg-type]
                            project=t["slug"] if t else None, **{"extension_card" if kind == "extension" else kind: obj})

    def inbox_entries(self, waiting: bool, extra: list[tuple[str, Any]]) -> list[s.InboxEntry]:
        where = "status='open'" if waiting else "status NOT IN ('open','withdrawn')"
        out = [self._entry("question", self.question_model(r)) for r in self.db.q(f"SELECT * FROM questions WHERE {where}")]
        out += [self._entry("permission", self.permission_model(r)) for r in self.db.q(f"SELECT * FROM permissions WHERE {where}")]
        out += [self._entry("task", self.task_model(r)) for r in self.db.q(f"SELECT * FROM tasks WHERE {where}")]
        out += [self._entry("suggestion", self.suggestion_model(r)) for r in self.db.q(f"SELECT * FROM suggestions WHERE {where}")]
        out += [self._entry("promote", self.promote_model(r)) for r in self.db.q(f"SELECT * FROM promote_requests WHERE {where}")]
        out += [self._entry(kind, obj) for kind, obj in extra]
        return sorted(out, key=lambda e: e.at, reverse=True)

    def counts(self) -> dict[str, int]:
        by = {}
        for kind, table in (("question", "questions"), ("permission", "permissions"), ("task", "tasks"), ("suggestion", "suggestions"),
                            ("promote", "promote_requests"), ("security", "security_alerts")):
            n = self.db.q1(f"SELECT COUNT(*) AS n FROM {table} WHERE status='open'")["n"]
            if n:
                by[kind] = n
        return by

    # ── delivery to the agent ──────────────────────────────────────────────
    def _where(self, thread_id: int | None) -> str:
        if not thread_id:
            return "(no thread)"
        t = self.db.q1("SELECT t.title, p.slug FROM threads t LEFT JOIN projects p ON p.id=t.project_id WHERE t.id=?", (thread_id,))
        return f"thread #{thread_id} “{t['title']}” ({t['slug'] or 'General'})" if t else f"thread #{thread_id}"

    def deliver(self, peek: bool = False) -> tuple[str, dict[str, int]]:
        """Everything new for the agent, as text, marked delivered unless peeking.
        Returns (text, counts by kind)."""
        now = self.db.now()
        groups: dict[str, list[str]] = {}
        counts: dict[str, int] = {}

        def add(thread_id: int | None, line: str, kind: str) -> None:
            groups.setdefault(self._where(thread_id), []).append(line)
            counts[kind] = counts.get(kind, 0) + 1

        msgs = self.db.q("SELECT * FROM messages WHERE delivered_at IS NULL AND sender IN ('owner','chat') ORDER BY id")
        for m in msgs:
            atts = self.db.q("SELECT path FROM attachments WHERE message_id=?", (m["id"],))
            extra = f"\n  (attached: {', '.join('/work/' + a['path'] for a in atts)})" if atts else ""
            body = m["body"].strip().replace("\n", "\n  ")
            add(m["thread_id"], f"- {_owner_label(m['via'], m['sender'])} (message #{m['id']}): {body}{extra}", "messages")
        qs = self.db.q("SELECT * FROM questions WHERE delivered_at IS NULL AND status='answered' ORDER BY id")
        for q in qs:
            note = f" Note: {q['note']}" if q["note"] else ""
            add(q["thread_id"], f"- Answer to your question #{q['id']} “{q['text']}”: {_show(_j(q['answer_json']))}.{note}", "answers")
        ps = self.db.q("SELECT * FROM permissions WHERE delivered_at IS NULL AND status IN ('allowed','denied') ORDER BY id")
        for p in ps:
            note = f" Note: {p['note']}" if p["note"] else ""
            how = f"allowed ({p['scope']}{'; ' + SCOPE_ONCE_NOTE if p['scope'] == 'once' else ''})" if p["status"] == "allowed" else "denied"
            add(p["thread_id"], f"- Permission #{p['id']} “{p['text']}”: {how}.{note}", "decisions")
        ts = self.db.q("SELECT * FROM tasks WHERE delivered_at IS NULL AND status IN ('done','declined') ORDER BY id")
        for t in ts:
            note = f" Note: {t['note']}" if t["note"] else ""
            add(t["thread_id"], f"- Task #{t['id']} “{t['text']}”: {'done' if t['status'] == 'done' else 'won’t do'}.{note}", "tasks")
        ss = self.db.q("SELECT * FROM suggestions WHERE delivered_at IS NULL AND status IN ('accepted','dismissed') ORDER BY id")
        for x in ss:
            add(x["target_id"] if x["target_kind"] == "thread" else None, f"- Your suggestion #{x['id']} ({x['kind']}): {x['status']}.", "suggestions")
        prs = self.db.q("SELECT * FROM promote_requests WHERE delivered_at IS NULL AND status IN ('approved','denied','expired') ORDER BY id")
        for pr in prs:
            note = f" Note: {pr['note']}" if pr["note"] else ""
            how = {"approved": f"approved: carry on as a full run on the main allowance (+{pr['budget']:g} pts)",
                   "denied": "denied: finish the reply, claim the thread and plan the work for a later wakeup",
                   "expired": "not answered in time: finish the reply, claim the thread and plan the work for a later wakeup"}[pr["status"]]
            add(pr["thread_id"], f"- Promote request #{pr['id']} “{pr['reason']}”: {how}.{note}", "decisions")
        rvs = self.db.q("SELECT * FROM reviewer_messages WHERE sender='reviewer' AND kind='reply' AND delivered_at IS NULL ORDER BY id")
        for rv in rvs:
            add(None, f"- The reviewer: {rv['body']}", "reviewer")
        secrets = self.db.q("SELECT id, name, description, scope FROM secrets WHERE announced_at IS NULL ORDER BY name")
        for sec in secrets:
            if sec["scope"] == "integrations":
                continue  # control's brokered tools only; the agent is told nothing (it's marked announced below)
            desc = f" ({sec['description']})" if sec["description"] else ""
            limit = ""
            kind, _, ref = sec["scope"].partition(":")
            if kind == "project":
                limit = f" Only in runs on project {self.vault.scope_label(sec['scope'])}'s threads."
            elif kind == "routine":
                limit = f" Only in runs of routine {self.vault.scope_label(sec['scope'])}."
            add(None, f"- New secret {sec['name']}{desc}: in your environment from the next session; now via `assistant secret get {sec['name']}`.{limit}", "secrets")
        notes = self.db.q("SELECT * FROM agent_notifications WHERE delivered_at IS NULL ORDER BY id")
        for n in notes:
            add(None, f"- Note to yourself ({n['created_at']}): {n['body']}", "notes")

        if not peek:
            with self.db.tx() as db:
                for table, rows in (("questions", qs), ("permissions", ps), ("tasks", ts), ("suggestions", ss), ("agent_notifications", notes),
                                    ("promote_requests", prs), ("reviewer_messages", rvs)):
                    for row in rows:
                        db.ex(f"UPDATE {table} SET delivered_at=? WHERE id=?", (now, row["id"]))
                for m in msgs:
                    db.ex("UPDATE messages SET delivered_at=?, delivery='delivered' WHERE id=?", (now, m["id"]))
                for sec in secrets:
                    db.ex("UPDATE secrets SET announced_at=? WHERE id=?", (now, sec["id"]))
            for m in msgs:
                self.live.changed("message", m["id"])
        text = "\n\n".join(f"{where}:\n" + "\n".join(lines) for where, lines in groups.items())
        if counts.get("messages"):
            text += "\n\n" + CLAIM_FIRST
        return text, counts

    def thread_history(self, thread_id: int, limit: int = 30) -> str:
        rows = self.db.q("SELECT * FROM (SELECT * FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (thread_id, limit))
        who = {"owner": "Owner", "assistant": "You", "chat": "Owner (via chat)", "system": "System"}

        def attached(m: Any) -> str:
            atts = self.db.q("SELECT path FROM attachments WHERE message_id=? ORDER BY id", (m["id"],))
            return f"\n(attached: {', '.join('/work/' + a['path'] for a in atts)})" if atts else ""

        return "\n\n".join(f"#{m['id']} {who.get(m['sender'], m['sender'])} ({m['created_at']}):\n{m['body'].strip()}{attached(m)}" for m in rows) or "(no messages yet)"

    # ── search ─────────────────────────────────────────────────────────────
    def search(self, q: str, kind: str | None, project: str | None, since: str | None) -> list[s.SearchHit]:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        # FTS5 (trigram: case-insensitive substring) for text of 3+ characters; shorter queries use LIKE.
        fts = '"' + q.replace('"', '""') + '"' if len(q) >= 3 else None
        hits: list[s.SearchHit] = []
        if kind in (None, "project"):
            for r in self.db.q("SELECT * FROM projects WHERE name LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR slug LIKE ? ESCAPE '\\' LIMIT 20", (like, like, like)):
                hits.append(s.SearchHit(kind="project", title=r["name"], snippet=r["summary"] or None, link=f"l0wagent://project/{r['slug']}", project=r["slug"], at=parse(r["updated_at"])))
        if kind in (None, "thread"):
            where, params = ("t.id IN (SELECT rowid FROM threads_fts WHERE threads_fts MATCH ?)", (fts,)) if fts else ("t.title LIKE ? ESCAPE '\\'", (like,))
            for r in self.db.q(f"SELECT t.*, p.slug AS project FROM threads t LEFT JOIN projects p ON p.id=t.project_id WHERE {where} ORDER BY t.updated_at DESC LIMIT 30", params):
                hits.append(s.SearchHit(kind="thread", title=r["title"], snippet="archived" if r["status"] == "archived" else None, link=f"l0wagent://thread/{r['id']}",
                                        project=r["project"], at=parse(r["updated_at"])))
        if kind in (None, "message"):
            where, params = ("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)", (fts,)) if fts else ("m.body LIKE ? ESCAPE '\\'", (like,))
            for r in self.db.q("SELECT m.*, t.title, p.slug AS project FROM messages m JOIN threads t ON t.id=m.thread_id LEFT JOIN projects p ON p.id=t.project_id "
                               f"WHERE {where} ORDER BY m.id DESC LIMIT 40", params):
                i = r["body"].lower().find(q.lower())
                snippet = ("…" if i > 40 else "") + r["body"][max(0, i - 40): i + 80].replace("\n", " ") + "…"
                hits.append(s.SearchHit(kind="message", title=r["title"], snippet=snippet, link=f"l0wagent://thread/{r['thread_id']}#m{r['id']}",
                                        project=r["project"], at=parse(r["created_at"])))
        if kind in (None, "run"):
            where, params = (("id IN (SELECT rowid FROM runs_fts WHERE runs_fts MATCH ?)", (fts,)) if fts else
                             ("description LIKE ? ESCAPE '\\' OR now_sentence LIKE ? ESCAPE '\\' OR result LIKE ? ESCAPE '\\'", (like, like, like)))
            for r in self.db.q(f"SELECT * FROM runs WHERE {where} ORDER BY id DESC LIMIT 20", params):
                hits.append(s.SearchHit(kind="run", title=f"#{r['id']} {r['description'] or r['why']}", snippet=r["now_sentence"] or r["status"],
                                        link=f"l0wagent://run/{r['id']}", project=None, at=parse(r["started_at"])))
        return [h for h in hits if (project is None or h.project == project) and (since is None or (h.at is not None and h.at.date().isoformat() >= since))]


# ── permission actions ────────────────────────────────────────────────────
def _check_action(a: dict) -> tuple[str, dict]:
    """Validate an action the agent proposes. Control writes the summary the owner sees."""
    kind = a.get("kind")
    if kind == "egress_rule":
        pattern, mode = str(a.get("pattern", "")).strip(), a.get("mode", "allow_ro")
        if not re.match(r"^(\*\.)?[A-Za-z0-9.\-]+(/\d{1,2})?(:\d{1,5})?$", pattern) or not re.search(r"[A-Za-z0-9]", re.sub(r"[:/].*$", "", pattern)):
            raise BadRequest("action", f"Not an egress pattern: {pattern!r} (a host, *.domain, host:port or CIDR).")
        if mode not in ("allow", "allow_ro"):
            raise BadRequest("action", "Egress rules are allow (read and write) or allow_ro (read-only, plain HTTP).")
        return kind, {"pattern": pattern, "mode": mode}
    if kind == "secret":
        from .vault import validate_name

        return kind, {"name": validate_name(str(a.get("name", ""))), "description": str(a.get("description", ""))[:200]}
    if kind == "setting":
        key = str(a.get("key", ""))
        if key not in SPECS:
            raise BadRequest("action", f"Unknown setting {key!r}.")
        if SPECS[key].who != "agent":
            raise BadRequest("action", f"{key} is the owner's to change; you can propose only settings that shape your own runs (hold time, reply and wakeup length).")
        try:
            value = cast(SPECS[key].kind, a.get("value"))
            check_range(key, value)
        except (TypeError, ValueError):
            raise BadRequest("action", f"Bad value for {key}.") from None
        return kind, {"key": key, "value": value}
    raise BadRequest("action", "Actions are egress_rule, secret or setting.")


def _action_summary(kind: str, p: dict) -> str:
    if kind == "egress_rule":
        return f"allow {'read-only (GET over plain HTTP)' if p.get('mode') == 'allow_ro' else 'full'} access to {p.get('pattern')}"
    if kind == "secret":
        return f"you enter the secret {p.get('name')}" + (f" ({p['description']})" if p.get("description") else "")
    if kind == "routine":
        return f"add the routine “{p.get('name')}”: {p.get('schedule_text')}, {p.get('tier')}, {p.get('budget', 0):g} pts each time"
    if kind == "mcp_call":
        args = json.dumps(p.get("args") or {}, sort_keys=True)
        return f"call {p.get('title') or p.get('integration')} → {p.get('tool')} with {args if len(args) <= 400 else args[:399] + '…'}"
    if kind == "setting":
        v = p.get("value")
        return f"set {p.get('key')} to {v:g}" if isinstance(v, float) else f"set {p.get('key')} to {v}"
    return kind


def _owner_label(via: str | None, sender: str) -> str:
    """Who the agent sees as the sender of an owner-side message, including the way it was posted."""
    via = via or ""
    if sender == "chat" or via.startswith("chat:"):
        return "Owner (via chat)"
    if via.startswith("connector:"):
        return f"Owner, via {via[10:]} (connector)"
    return "Owner"


def _show(v: Any) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    return str(v)
