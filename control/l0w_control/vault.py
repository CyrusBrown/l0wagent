"""Secrets the owner gives the agent: write-only for people, redacted everywhere.

- Values are encrypted at rest (Fernet; the key is L0W_SECRET_KEY or `secret.key` in the
  data dir, created on first use). No API returns a value to a person.
- Every session gets every secret as an environment variable (scope 'agent', the only one
  until M6); `assistant secret get NAME` reads one and is logged.
- `redact()` replaces any secret value (8+ characters) with `[secret:NAME]`. Control applies
  it to everything it stores or shows from the agent or the owner, and setting a secret
  scrubs it from what's already stored.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from cryptography.fernet import Fernet

from .backends.base import BadRequest, NotFound
from .db import Database

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MIN_REDACT_LEN = 8
# (table, column) pairs that can hold text from the agent or the owner.
SCRUB = [("messages", "body"), ("run_events", "raw_json"), ("timeline_items", "text"), ("timeline_items", "data_json"),
         ("questions", "text"), ("questions", "note"), ("permissions", "text"), ("permissions", "reason"), ("permissions", "note"),
         ("tasks", "text"), ("tasks", "detail"), ("tasks", "note"), ("claims", "plan"), ("claims", "result"), ("suggestions", "text"),
         ("agent_notifications", "body"), ("run_notes", "text"), ("runs", "now_sentence"), ("runs", "description"), ("runs", "result")]


def validate_name(name: str) -> str:
    name = (name or "").strip().upper()
    if not NAME_RE.match(name):
        raise BadRequest("bad_name", "Secret names look like JELLYFIN_API_KEY: capital letters, digits and _, starting with a letter.")
    return name


class Vault:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        key = os.environ.get("L0W_SECRET_KEY", "").encode()
        if not key:
            path = data_dir / "secret.key"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Fernet.generate_key())
                path.chmod(0o600)
            key = path.read_bytes().strip()
        self.fernet = Fernet(key)
        self._lock = threading.Lock()
        self._values: list[tuple[str, str]] | None = None  # (value, name), longest first

    # ── values (never returned to people) ──────────────────────────────────
    def _all(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._values is None:
                rows = self.db.q("SELECT name, value_enc FROM secrets")
                vals = [(self.fernet.decrypt(r["value_enc"]).decode(), r["name"]) for r in rows]
                self._values = sorted(vals, key=lambda t: -len(t[0]))
            return self._values

    def redact(self, text: str | None) -> str | None:
        if not text:
            return text
        for value, name in self._all():
            if len(value) >= MIN_REDACT_LEN and value in text:
                text = text.replace(value, f"[secret:{name}]")
        return text

    def env(self) -> dict[str, str]:
        """Every secret, for a session's environment."""
        return {name: value for value, name in self._all()}

    def get_for_agent(self, name: str, run_id: int | None) -> str:
        name = (name or "").strip().upper()
        row = self.db.q1("SELECT id, value_enc FROM secrets WHERE name=?", (name,))
        if not row:
            raise NotFound(f"secret {name}")
        now = self.db.now()
        self.db.ex("UPDATE secrets SET reads=reads+1, last_read_at=? WHERE id=?", (now, row["id"]))
        self.db.ex("INSERT INTO secret_reads(secret_id, run_id, at, how) VALUES(?,?,?,'get')", (row["id"], run_id, now))
        return self.fernet.decrypt(row["value_enc"]).decode()

    # ── management (names and metadata only) ───────────────────────────────
    def listing(self) -> list[dict]:
        return self.db.q("SELECT name, description, scope, created_at, updated_at, reads, last_read_at FROM secrets ORDER BY name")

    def exists(self, name: str) -> bool:
        return self.db.q1("SELECT 1 FROM secrets WHERE name=?", (name.upper(),)) is not None

    def set(self, name: str, value: str, description: str | None = None) -> dict:
        name = validate_name(name)
        value = value.strip()
        if not value:
            raise BadRequest("empty_value", "The secret's value is empty.")
        now = self.db.now()
        existing = self.db.q1("SELECT description FROM secrets WHERE name=?", (name,))
        desc = (description if description is not None else (existing or {}).get("description")) or ""
        self.db.ex(
            "INSERT INTO secrets(name, description, value_enc, created_at, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value_enc=excluded.value_enc, description=excluded.description, updated_at=excluded.updated_at, announced_at=NULL",
            (name, desc.strip(), self.fernet.encrypt(value.encode()), now, now),
        )
        with self._lock:
            self._values = None
        scrubbed = self.scrub(name, value) if len(value) >= MIN_REDACT_LEN else 0
        return {"name": name, "created": existing is None, "scrubbed": scrubbed}

    def delete(self, name: str) -> None:
        name = name.strip().upper()
        row = self.db.q1("SELECT id FROM secrets WHERE name=?", (name,))
        if not row:
            raise NotFound(f"secret {name}")
        with self.db.tx() as db:
            db.ex("DELETE FROM secret_reads WHERE secret_id=?", (row["id"],))
            db.ex("DELETE FROM secrets WHERE id=?", (row["id"],))
        with self._lock:
            self._values = None

    def scrub(self, name: str, value: str) -> int:
        """Replace a value wherever control already stored it (e.g. pasted into a thread)."""
        tag, total = f"[secret:{name}]", 0
        with self.db.tx() as db:
            for table, col in SCRUB:
                total += db.ex(f"UPDATE {table} SET {col}=REPLACE({col}, ?, ?) WHERE instr({col}, ?) > 0", (value, tag, value))
        return total
