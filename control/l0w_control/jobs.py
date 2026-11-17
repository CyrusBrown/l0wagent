"""The job registry: what the agent runs in the background.

The agent registers a long job with `assistant job start "label" [--pid N] [--log PATH]
[--expect 2h] [--dont-interrupt]`, updates its progress, and marks it done (usually as the
job's last command, chained with `assistant notify`). Control shows running jobs on Home and
the Runs page with their log tail, read safely from the agent's storage. If a job with a pid
stops without being marked done, control marks it lost and tells the agent. A job marked
"don't interrupt" holds updates (M5) until it ends.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, NotFound
from .db import Database
from .files import Storage
from .live import Live

log = logging.getLogger("l0w.jobs")
TAIL_BYTES = 4000
VOLUMES = {"/work/": "work", "/workspace/": "workspace"}


def split_path(path: str | None) -> tuple[str, str] | None:
    """'/work/garden/train.log' → ('work', 'garden/train.log'); anything else → None."""
    if not path:
        return None
    for prefix, vol in VOLUMES.items():
        if path.startswith(prefix):
            return vol, path[len(prefix):]
    return None


class Jobs:
    def __init__(self, db: Database, live: Live, storage: Storage, redact):
        self.db, self.live, self.storage, self.redact = db, live, storage, redact

    def _tail(self, log_path: str | None) -> str:
        where = split_path(log_path)
        if not where:
            return ""
        try:
            size = self.storage.stat(*where).st_size
            data, _ = self.storage.read_range(where[0], where[1], max(0, size - TAIL_BYTES), size)
        except Exception as e:  # noqa: BLE001
            return f"(can't read the log: {e})"
        text = data.decode("utf-8", "replace")
        if size > TAIL_BYTES and "\n" in text:
            text = text.split("\n", 1)[1]
        return self.redact(text) or ""

    def model(self, r: dict) -> s.Job:
        return s.Job(id=r["id"], run_id=r["run_id"], label=r["label"], progress=r["progress"], note=r["note"], expected_end=parse(r["expected_end"]),
                     dont_interrupt=bool(r["dont_interrupt"]), status=r["status"], started_at=parse(r["started_at"]), log_tail=self._tail(r["log_path"]))  # type: ignore[arg-type]

    def list(self, include_done: bool) -> list[s.Job]:
        rows = self.db.q("SELECT * FROM jobs WHERE (? OR status='running') ORDER BY id DESC LIMIT 100", (int(include_done),))
        return [self.model(r) for r in rows]

    def get(self, jid: int) -> s.Job:
        r = self.db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise NotFound(f"job {jid}")
        return self.model(r)

    def start(self, run_id: int | None, label: str, pid: int | None, log_path: str | None, expect_minutes: float | None, dont_interrupt: bool) -> s.Job:
        if log_path and not split_path(log_path):
            raise BadRequest("bad_log", "Put job logs under /work/… or /workspace/… so the owner can see them.")
        now = utcnow()
        end = iso(now + timedelta(minutes=expect_minutes)) if expect_minutes else None
        jid = self.db.ex("INSERT INTO jobs(run_id, label, pid, log_path, expected_end, dont_interrupt, status, started_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (run_id, self.redact(label.strip()), pid, log_path, end, int(dont_interrupt), "running", iso(now), iso(now)))
        self.live.emit("job", {"id": jid})
        return self.get(jid)

    def update(self, jid: int, progress: str | None, status: str | None, note: str | None) -> s.Job:
        r = self.db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise NotFound(f"job {jid}")
        if status and status not in ("running", "done", "failed"):
            raise BadRequest("bad_status", "A job is running, done or failed.")
        ended = self.db.now() if status in ("done", "failed") else None
        self.db.ex("UPDATE jobs SET progress=COALESCE(?, progress), status=COALESCE(?, status), note=COALESCE(?, note), ended_at=COALESCE(?, ended_at), updated_at=? WHERE id=?",
                   (self.redact(progress), status, self.redact(note), ended, self.db.now(), jid))
        self.live.emit("job", {"id": jid})
        return self.get(jid)

    def mark_lost(self, alive: set[int]) -> list[s.Job]:
        """Jobs whose process is gone but that were never marked done."""
        lost = []
        for r in self.db.q("SELECT * FROM jobs WHERE status='running' AND pid IS NOT NULL"):
            if r["pid"] not in alive:
                self.db.ex("UPDATE jobs SET status='lost', ended_at=?, updated_at=? WHERE id=?", (self.db.now(), self.db.now(), r["id"]))
                self.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(NULL, ?, ?)",
                           (f"[control] Job #{r['id']} “{r['label']}” (pid {r['pid']}) stopped without `assistant job done`. Check its log"
                            + (f" ({r['log_path']})" if r["log_path"] else "") + ".", self.db.now()))
                self.live.emit("job", {"id": r["id"]})
                lost.append(self.get(r["id"]))
        return lost

    def running_pids(self) -> list[int]:
        return [r["pid"] for r in self.db.q("SELECT pid FROM jobs WHERE status='running' AND pid IS NOT NULL")]

    def holding_updates(self) -> list[s.Job]:
        return [self.model(r) for r in self.db.q("SELECT * FROM jobs WHERE status='running' AND dont_interrupt=1")]

    def brief_text(self) -> str:
        rows = self.db.q("SELECT * FROM jobs WHERE status='running' ORDER BY id")
        out = []
        for r in rows:
            end = f", expected by {r['expected_end']}" if r["expected_end"] else ""
            if r["expected_end"] and r["expected_end"] < self.db.now():
                end += " (past its estimate: check it's still progressing; `assistant job progress ID TEXT` keeps the owner informed)"
            prog = f": {r['progress']}" if r["progress"] else ""
            out.append(f"- job #{r['id']} “{r['label']}” since {r['started_at']}{end}{prog}" + (" (don't interrupt)" if r["dont_interrupt"] else ""))
        return "\n".join(out)
