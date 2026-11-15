"""The live stream and the sync feed.

Every change to an entity the UI caches goes through `changed()`: it appends to
`change_log` (whose `seq` is the sync cursor) and pushes a LiveEvent to every open
`GET /api/events` stream. `emit()` pushes an event that doesn't change cached data
(heartbeats, status). Both are safe to call from request threads and the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable

from .api import schemas as s
from .db import Database

HEARTBEAT_S = 15  # the web app reconnects after 40 s of silence
QUEUE_MAX = 1000
# Entity name → live event type, where they differ.
EVENT_TYPE = {"timeline_item": "timeline", "security_alert": "security", "auth_session": "status"}

Getter = Callable[[str], s.Model | None]


class Live:
    def __init__(self, db: Database):
        self.db = db
        self.loop: asyncio.AbstractEventLoop | None = None
        self._listeners: set[asyncio.Queue[s.LiveEvent]] = set()
        self._lock = threading.Lock()
        self.getters: dict[str, Getter] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # ── producing ──────────────────────────────────────────────────────────
    def changed(self, entity: str, entity_id: object, deleted: bool = False) -> int:
        op = "delete" if deleted else "upsert"
        seq = self.db.ex("INSERT INTO change_log(entity, entity_id, op, at) VALUES(?,?,?,?)", (entity, str(entity_id), op, self.db.now()))
        change = self._change(entity, str(entity_id), op)
        self.emit(EVENT_TYPE.get(entity, entity), change.model_dump(mode="json"), cursor=str(seq))
        return seq

    def emit(self, type_: str, data: dict | None = None, cursor: str | None = None) -> None:
        event = s.LiveEvent(type=type_, cursor=cursor, data=data or {})  # type: ignore[arg-type]
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(event)
        else:
            loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: s.LiveEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            if q.full():  # a stuck client: drop it; it reconnects and refetches
                with self._lock:
                    self._listeners.discard(q)
                continue
            q.put_nowait(event)

    # ── consuming ──────────────────────────────────────────────────────────
    async def stream(self) -> AsyncIterator[s.LiveEvent]:
        q: asyncio.Queue[s.LiveEvent] = asyncio.Queue(QUEUE_MAX)
        with self._lock:
            self._listeners.add(q)
        try:
            yield s.LiveEvent(type="hello", cursor=self.cursor(), data={})
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), HEARTBEAT_S)
                except TimeoutError:
                    with self._lock:
                        if q not in self._listeners:  # dropped as stuck
                            return
                    yield s.LiveEvent(type="heartbeat", data={})
        finally:
            with self._lock:
                self._listeners.discard(q)

    # ── sync feed ──────────────────────────────────────────────────────────
    def cursor(self) -> str:
        return str(self.db.q1("SELECT COALESCE(MAX(seq), 0) AS n FROM change_log")["n"])

    def _change(self, entity: str, entity_id: str, op: str) -> s.SyncChange:
        model = None
        if op == "upsert" and entity in self.getters:
            try:
                model = self.getters[entity](entity_id)
            except Exception:  # noqa: BLE001 - e.g. NotFound after a race; report as deleted
                model = None
            if model is None:
                op = "delete"
        return s.SyncChange(entity=entity, id=entity_id, op=op, data=model.model_dump(mode="json") if model else None)  # type: ignore[arg-type]

    def sync(self, since: str | None, snapshot: Callable[[], list[tuple[str, str]]]) -> s.SyncResponse:
        cursor = self.cursor()
        if since is None or not since.isdigit():
            return s.SyncResponse(cursor=cursor, changes=[self._change(e, i, "upsert") for e, i in snapshot()], reset=since is not None)
        rows = self.db.q("SELECT entity, entity_id, MAX(seq) AS seq FROM change_log WHERE seq > ? GROUP BY entity, entity_id ORDER BY seq", (int(since),))
        return s.SyncResponse(cursor=cursor, changes=[self._change(r["entity"], r["entity_id"], "upsert") for r in rows])
