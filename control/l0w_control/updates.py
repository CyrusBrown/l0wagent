"""Updates: the "Update available" check, and draining for `./l0w update`.

- **The check**: every few hours control asks GitHub for the latest release on the stable
  channel and compares it with its own version. The UI shows a banner with the release notes;
  applying it is `./l0w update` on the host (no Docker socket in control). The edge channel
  follows main, so it has no banner: every `./l0w update` takes the newest build.
- **Draining** (`./l0w update` turns it on through `python -m l0w_control.hostctl drain on`):
  no new run starts (wakeups wait, Reply now and Wake now are refused), the current run
  finishes, and jobs marked "don't interrupt" are waited for. The flag lives in the database
  with an expiry, so it survives the restart and can't strand the assistant.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import httpx

from . import __version__
from .api import schemas as s
from .auth import iso, parse, utcnow
from .db import Database

log = logging.getLogger("l0w.updates")
RELEASES_URL = os.environ.get("L0W_UPDATE_URL", "https://api.github.com/repos/CyrusBrown/l0wagent/releases/latest")
CHECK_EVERY = timedelta(hours=6)
DRAIN_KEY = "drain_until"


def _ver(v: str | None) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def draining(db: Database) -> datetime | None:
    row = db.q1("SELECT value FROM kv WHERE key=?", (DRAIN_KEY,))
    until = parse(row["value"]) if row else None
    return until if until and until > utcnow() else None


def set_drain(db: Database, on: bool, minutes: float = 120) -> None:
    if on:
        db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
              (DRAIN_KEY, iso(utcnow() + timedelta(minutes=minutes)), db.now()))
    else:
        db.ex("DELETE FROM kv WHERE key=?", (DRAIN_KEY,))


def blockers(db: Database) -> list[str]:
    """What an update is waiting for."""
    out = [f"run #{r['id']} ({r['kind']}) is in progress" for r in db.q("SELECT id, kind FROM runs WHERE status IN ('starting','running')")]
    out += [f"job #{r['id']} “{r['label']}” is marked don't interrupt" for r in db.q("SELECT id, label FROM jobs WHERE status='running' AND dont_interrupt=1")]
    return out


class Updates:
    def __init__(self, db: Database):
        self.db = db

    def channel(self) -> str:
        return os.environ.get("L0W_CHANNEL", "stable")

    def _cached(self) -> dict:
        row = self.db.q1("SELECT value FROM kv WHERE key='update_check'")
        return json.loads(row["value"]) if row else {}

    async def check(self, force: bool = False) -> dict:
        cached = self._cached()
        if not force and cached.get("checked_at") and parse(cached["checked_at"]) > utcnow() - CHECK_EVERY:  # type: ignore[operator]
            return cached
        result: dict = {"checked_at": iso(utcnow()), "latest": None, "notes": None, "url": None, "error": None}
        try:
            async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/vnd.github+json", "User-Agent": "l0wagent-update-check"}) as c:
                r = await c.get(RELEASES_URL)
            if r.status_code == 200:
                data = r.json()
                result.update(latest=(data.get("tag_name") or "").lstrip("v") or None, notes=(data.get("body") or "")[:8000], url=data.get("html_url"))
            elif r.status_code != 404:  # 404: no releases yet
                result["error"] = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            result["error"] = str(e)[:200]
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('update_check', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (json.dumps(result), self.db.now()))
        return result

    def info(self) -> s.UpdateInfo:
        c = self._cached()
        cur, latest = _ver(__version__), _ver(c.get("latest"))
        available = self.channel() == "stable" and bool(cur and latest and latest > cur)
        until = draining(self.db)
        return s.UpdateInfo(current=__version__, channel=self.channel(), latest=c.get("latest"), available=available, notes=c.get("notes") if available else None,  # type: ignore[arg-type]
                            url=c.get("url"), checked_at=parse(c.get("checked_at")), error=c.get("error"), draining_until=until, waiting_for=blockers(self.db))
