"""The daily clean-up of tables that only grow: idempotency keys, login attempts, expired OAuth
rows and the raw run event log (the normalised timeline_items stay)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .auth import iso
from .config import Settings
from .db import Database

log = logging.getLogger("l0w.retention")
EVERY = timedelta(hours=24)
KEYS_DAYS = 7
LOGIN_DAYS = 30


def run_retention(db: Database, settings: Settings, now: datetime, force: bool = False) -> dict[str, int]:
    """Delete what's old. Runs at most once a day unless forced; returns the rows deleted per table."""
    last = db.q1("SELECT value FROM kv WHERE key='retention_last'")
    if last and not force and last["value"] > iso(now - EVERY):
        return {}
    keep = settings.get("RUN_EVENTS_KEEP_DAYS")
    ts = iso(now)
    deleted = {
        "idempotency_keys": db.ex("DELETE FROM idempotency_keys WHERE created_at<?", (iso(now - timedelta(days=KEYS_DAYS)),)),
        # keep each address's latest successful login: auth exempts known addresses from the global slow-down
        "login_attempts": db.ex("DELETE FROM login_attempts WHERE at<? AND id NOT IN (SELECT MAX(id) FROM login_attempts WHERE ok=1 GROUP BY ip)",
                                (iso(now - timedelta(days=LOGIN_DAYS)),)),
        "oauth_codes": db.ex("DELETE FROM oauth_codes WHERE expires_at<?", (ts,)),
        "oauth_tokens": db.ex("DELETE FROM oauth_tokens WHERE expires_at<?", (ts,)),
        # never a run that's still going: its log is what re-attaching after a restart reads
        "run_events": db.ex("DELETE FROM run_events WHERE at<? AND run_id IN (SELECT id FROM runs WHERE status NOT IN ('starting', 'running'))",
                            (iso(now - timedelta(days=keep)),)),
    }
    db.ex("INSERT INTO kv(key, value, updated_at) VALUES('retention_last', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (ts, db.now()))
    if any(deleted.values()):
        log.info("retention deleted %s", {k: v for k, v in deleted.items() if v})
    return deleted
