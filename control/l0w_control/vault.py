"""Secrets the owner gives the agent: write-only for people, redacted everywhere.

- Values are encrypted at rest (Fernet; the key is L0W_SECRET_KEY or `secret.key` in the
  data dir, created on first use). No API returns a value to a person.
- Scopes (M6): 'agent' (every worker session), 'project:<slug>' (only runs on that project's
  threads), 'routine:N' (only that routine's runs) and 'integrations' (never in the sandbox;
  only control's brokered tools use it). The reviewer, the 1:1 chat and the connector never
  get values. A session's environment holds the secrets in its scope; `assistant secret get
  NAME` reads one (scope-checked) and is logged.
- `redact()` replaces any secret value (8+ characters), and its base64, URL-encoded and JSON-escaped forms, with `[secret:NAME]`. Control applies
  it to everything it stores or shows from the agent or the owner, and setting a secret
  scrubs it from what's already stored.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet

from .backends.base import BadRequest, NotFound
from .db import Database

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MIN_REDACT_LEN = 8
# (table, column) pairs that can hold text from the agent or the owner.
SCRUB = [("messages", "body"), ("run_events", "raw_json"), ("timeline_items", "text"), ("timeline_items", "data_json"),
         ("questions", "text"), ("questions", "note"), ("permissions", "text"), ("permissions", "reason"), ("permissions", "note"),
         ("tasks", "text"), ("tasks", "detail"), ("tasks", "note"), ("claims", "plan"), ("claims", "result"), ("suggestions", "text"),
         ("agent_notifications", "body"), ("run_notes", "text"), ("runs", "now_sentence"), ("runs", "description"), ("runs", "result"),
         ("chat_messages", "body"), ("chat_messages", "data_json"), ("chat_messages", "raw_json"), ("chats", "title"),
         ("reviewer_messages", "body"), ("integration_calls", "args_json"), ("integration_calls", "detail"), ("wakeups", "reason"),
         ("jobs", "label"), ("jobs", "progress"), ("jobs", "note"), ("projects", "name"), ("projects", "summary"),
         ("threads", "title"), ("notifications", "title"), ("notifications", "body"),
         ("questions", "answer_json"), ("questions", "options_json"), ("questions", "default_json"), ("suggestions", "payload_json"),
         ("permission_actions", "payload_json"), ("permission_actions", "error"), ("security_alerts", "text"), ("routines", "name"),
         ("routines", "prompt"), ("runs", "why_detail"), ("runs", "stop_reason"), ("wakeups", "status_reason"), ("shelf_items", "label"),
         ("integrations", "last_error"), ("secrets", "description"), ("promote_requests", "reason"), ("promote_requests", "note")]


def forms(value: str) -> list[str]:
    """A value and the encodings it usually shows up in (base64, URL-encoded, JSON-escaped), longest first."""
    out = {value}
    raw = value.encode()
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        b = enc(raw).decode()
        out.update((b, b.rstrip("=")))
    out.add(quote(value, safe=""))
    out.add(json.dumps(value)[1:-1])
    return sorted((f for f in out if len(f) >= MIN_REDACT_LEN), key=lambda f: -len(f))


def store_text(owner, text: str | None) -> str | None:
    """The one way to prepare agent or owner text for storage: redact it. `owner` is a Vault or anything with `.vault`."""
    return getattr(owner, "vault", owner).redact(text)


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
        self.key = key
        self.fernet = Fernet(key)
        self._lock = threading.Lock()
        self._forms: list[tuple[str, str]] = []
        self._values: list[tuple[str, str]] | None = None  # (value, name), longest first

    # ── values (never returned to people) ──────────────────────────────────
    def _all(self) -> list[tuple[str, str]]:
        with self._lock:
            if self._values is None:
                rows = self.db.q("SELECT name, value_enc FROM secrets")
                vals = [(self.fernet.decrypt(r["value_enc"]).decode(), r["name"]) for r in rows]
                self._forms = sorted(((f, n) for v, n in vals if len(v) >= MIN_REDACT_LEN for f in forms(v)), key=lambda t: -len(t[0]))
                self._values = sorted(vals, key=lambda t: -len(t[0]))
            return self._values

    def redact(self, text: str | None) -> str | None:
        if not text:
            return text
        self._all()
        for form, name in self._forms:
            if form in text:
                text = text.replace(form, f"[secret:{name}]")
        return text

    # ── scopes ─────────────────────────────────────────────────────────────
    def _run_ctx(self, run_id: int | None) -> dict | None:
        if run_id is None:
            return None
        return self.db.q1("SELECT r.kind, r.routine_id, p.slug AS project FROM runs r LEFT JOIN threads t ON t.id = r.thread_id "
                          "LEFT JOIN projects p ON p.id = t.project_id WHERE r.id=?", (run_id,))

    def in_scope(self, scope: str, run_id: int | None) -> bool:
        """Whether a session of this run may see a secret with this scope."""
        ctx = self._run_ctx(run_id)
        if ctx and ctx["kind"] == "reviewer":
            return False
        if scope == "agent":
            return True
        kind, _, ref = scope.partition(":")
        if not ctx or not ref:
            return False
        if kind == "project":
            return ctx["project"] == ref
        if kind == "routine":
            return str(ctx["routine_id"]) == ref
        return False  # 'integrations' and anything unknown

    def validate_scope(self, scope: str) -> str:
        scope = (scope or "").strip().lower()
        if scope in ("agent", "integrations"):
            return scope
        if self._scope_row(scope):
            return scope
        raise BadRequest("bad_scope", "A secret's scope is 'agent', 'integrations', 'project:<slug>' or 'routine:<id>' of an existing project or routine.")

    def _scope_row(self, scope: str) -> dict | None:
        kind, _, ref = scope.partition(":")
        if kind == "project" and ref:
            return self.db.q1("SELECT name FROM projects WHERE slug=?", (ref,))
        if kind == "routine" and ref.isdigit():
            return self.db.q1("SELECT name FROM routines WHERE id=?", (int(ref),))
        return None

    def scope_label(self, scope: str) -> str:
        if scope == "agent":
            return "All projects"
        if scope == "integrations":
            return "Integrations only"
        row = self._scope_row(scope)
        return row["name"] if row else f"{scope} (deleted)"

    def env(self, run_id: int | None) -> dict[str, str]:
        """The secrets in scope for a session of this run, for its environment."""
        scopes = {r["name"]: r["scope"] for r in self.db.q("SELECT name, scope FROM secrets")}
        return {name: value for value, name in self._all() if self.in_scope(scopes.get(name, "integrations"), run_id)}

    def value_for_control(self, name: str) -> str | None:
        """A value for control's own use (brokered tools). Never goes to the sandbox or a model."""
        row = self.db.q1("SELECT value_enc FROM secrets WHERE name=?", ((name or "").strip().upper(),))
        return self.fernet.decrypt(row["value_enc"]).decode() if row else None

    def get_for_agent(self, name: str, run_id: int | None) -> str:
        name = (name or "").strip().upper()
        row = self.db.q1("SELECT id, value_enc, scope FROM secrets WHERE name=?", (name,))
        if not row or not self.in_scope(row["scope"], run_id):
            raise NotFound(f"secret {name}")
        now = self.db.now()
        self.db.ex("UPDATE secrets SET reads=reads+1, last_read_at=? WHERE id=?", (now, row["id"]))
        self.db.ex("INSERT INTO secret_reads(secret_id, run_id, at, how) VALUES(?,?,?,'get')", (row["id"], run_id, now))
        return self.fernet.decrypt(row["value_enc"]).decode()

    # ── management (names and metadata only) ───────────────────────────────
    def listing(self) -> list[dict]:
        return self.db.q("SELECT name, description, scope, created_at, updated_at, reads, last_read_at FROM secrets ORDER BY name")

    def listing_for_agent(self, run_id: int | None) -> list[dict]:
        return [r for r in self.listing() if self.in_scope(r["scope"], run_id)]

    def exists(self, name: str) -> bool:
        return self.db.q1("SELECT 1 FROM secrets WHERE name=?", (name.upper(),)) is not None

    def set(self, name: str, value: str | None, description: str | None = None, scope: str | None = None) -> dict:
        """Save a value, or (value None) change an existing secret's description or scope."""
        name = validate_name(name)
        now = self.db.now()
        existing = self.db.q1("SELECT description, scope FROM secrets WHERE name=?", (name,))
        desc = (description if description is not None else (existing or {}).get("description")) or ""
        scope = self.validate_scope(scope) if scope is not None else (existing or {}).get("scope", "agent")
        if value is None:
            if not existing:
                raise NotFound(f"secret {name}")
            self.db.ex("UPDATE secrets SET description=?, scope=?, updated_at=? WHERE name=?", (desc.strip(), scope, now, name))
            return {"name": name, "created": False, "scrubbed": 0}
        value = value.strip()
        if not value:
            raise BadRequest("empty_value", "The secret's value is empty.")
        self.db.ex(
            "INSERT INTO secrets(name, description, value_enc, scope, created_at, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value_enc=excluded.value_enc, description=excluded.description, scope=excluded.scope, "
            "updated_at=excluded.updated_at, announced_at=NULL",
            (name, desc.strip(), self.fernet.encrypt(value.encode()), scope, now, now),
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
