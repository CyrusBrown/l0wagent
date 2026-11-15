"""SQLite access and versioned migrations (no ORM).

Migrations are `migrations/NNNN_name.sql`, applied in order inside one
transaction each. The applied version is kept in `schema_version`.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def migrations() -> list[tuple[int, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        m = _NAME.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration numbers must be 1..N without gaps, got {versions}")
    return found


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)  # explicit transactions; Database locks
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting version."""
    version = current_version(conn)
    for number, path in migrations():
        if number <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN")
        try:
            # executescript would commit implicitly; run statements one by one instead.
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (number, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version = number
    return version


def _split(sql: str) -> list[str]:
    """Split a migration into statements. Migrations must not contain ';' inside literals or triggers."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """One connection shared by the request threads and the event loop, behind a lock.

    `q`/`q1` read, `ex` runs one statement (autocommit), and `tx()` groups statements
    into one transaction. Everything returns plain dicts.
    """

    def __init__(self, path: str | Path):
        import threading

        self.path = str(path)
        self.conn = connect(self.path)
        self.lock = threading.RLock()
        with self.lock:
            migrate(self.conn)

    now = staticmethod(now)

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def q1(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def ex(self, sql: str, params: tuple | list = ()) -> int:
        """Run one statement; return lastrowid (for inserts) or rowcount."""
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def tx(self):
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> Database:
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()

