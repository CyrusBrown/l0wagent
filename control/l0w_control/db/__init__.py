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
    """Split a migration into statements. ';' inside a trigger body is kept (sqlite3.complete_statement
    decides where a statement ends); migrations must not put '--' or ';' inside string literals."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    out, buf = [], ""
    for piece in no_comments.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            out.append(buf.strip().rstrip(";").strip())
            buf = ""
    if buf.strip(" ;\n"):
        out.append(buf.strip().rstrip(";").strip())
    return [s for s in out if s]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


READERS = 4


class Database:
    """One writer connection behind a lock, plus a small pool of read-only connections.

    `q`/`q1` read, `ex` runs one statement (autocommit), and `tx()` groups statements
    into one transaction. Everything returns plain dicts. Reads inside a `tx()` on the
    same thread use the writer, so they see the transaction's own rows; other reads use
    the pool (WAL: they see what is committed and never wait for a writer). An in-memory
    database can't be shared between connections, so it has no pool.
    """

    def __init__(self, path: str | Path):
        import queue
        import threading

        self.path = str(path)
        self.conn = connect(self.path)
        self.lock = threading.RLock()
        self._tx_owner: int | None = None
        self._readers: queue.Queue | None = None
        with self.lock:
            migrate(self.conn)
        if ":memory:" not in self.path and not self.path.startswith("file:") and self.path:
            self._readers = queue.Queue()
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            for _ in range(READERS):
                r = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
                r.row_factory = sqlite3.Row
                r.execute("PRAGMA busy_timeout = 5000")
                r.execute("PRAGMA query_only = ON")
                self._readers.put(r)

    now = staticmethod(now)

    def _read(self, sql: str, params: tuple | list, one: bool):
        import threading

        if self._readers is None or self._tx_owner == threading.get_ident():
            with self.lock:
                cur = self.conn.execute(sql, params)
                return cur.fetchone() if one else cur.fetchall()
        r = self._readers.get()
        try:
            cur = r.execute(sql, params)
            return cur.fetchone() if one else cur.fetchall()
        finally:
            self._readers.put(r)

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self._read(sql, params, False)]

    def q1(self, sql: str, params: tuple | list = ()) -> dict | None:
        row = self._read(sql, params, True)
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
        import threading

        self.db.lock.acquire()
        try:
            self.db.conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.db.lock.release()
            raise
        self.prev = self.db._tx_owner
        self.db._tx_owner = threading.get_ident()
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._tx_owner = self.prev
            self.db.lock.release()

