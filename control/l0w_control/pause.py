"""The owner's emergency pause (Settings → System).

While it's on: the current run is stopped, no run starts (wakeups, routines, Reply now, Wake
now), and wakeups that fall due are skipped rather than left to pile up for the moment it ends.
The 1:1 chat and the connector are the owner's own and keep working. It ends at its time or
when the owner resumes; the scheduler then tells the agent what was skipped.

The state is one kv row, like the update drain, so it survives restarts.
"""

from __future__ import annotations

import json
from datetime import datetime

from .api import schemas as s
from .auth import iso, parse
from .db import Database

KEY = "pause"


def raw(db: Database) -> dict | None:
    row = db.q1("SELECT value FROM kv WHERE key=?", (KEY,))
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def expired(p: dict, now: datetime) -> bool:
    until = parse(p.get("until"))
    return bool(until and until <= now)


def active(db: Database, now: datetime) -> dict | None:
    """The pause in force at `now`, or None (one past its end counts as over)."""
    p = raw(db)
    return p if p and not expired(p, now) else None


def _store(db: Database, p: dict) -> None:
    db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
          (KEY, json.dumps(p), db.now()))


def start(db: Database, now: datetime, until: datetime | None) -> dict:
    """Pause, or change the end of a pause already on (keeping its start and what it skipped)."""
    p = active(db, now) or {"since": iso(now), "skipped": []}
    p["until"] = iso(until) if until else None
    _store(db, p)
    return p


def add_skipped(db: Database, wakeup_id: int) -> None:
    p = raw(db)
    if p is not None:
        p["skipped"] = [*p.get("skipped", []), wakeup_id]
        _store(db, p)


def clear(db: Database) -> dict | None:
    p = raw(db)
    db.ex("DELETE FROM kv WHERE key=?", (KEY,))
    return p


def model(p: dict | None) -> s.PauseState:
    if not p:
        return s.PauseState(paused=False)
    return s.PauseState(paused=True, since=parse(p.get("since")), until=parse(p.get("until")), skipped=len(p.get("skipped", [])))
