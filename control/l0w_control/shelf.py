"""The shelf: deliverables put in front of the owner, per project.

- **Live** items are always the current file. Control watches them; when one changes it
  updates the item (the UI refreshes live) and, if the item keeps versions, stores a copy.
- **Snapshot** items are copied when shelved.
Copies live in control's own data dir (`shelf/<item>/<n>`), never in the agent's storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path

from .api import schemas as s
from .auth import parse
from .backends.base import BadRequest, Conflict, NotFound
from .db import Database
from .files import Storage, guess_mime
from .live import Live

log = logging.getLogger("l0w.shelf")
POLL_S = 2.0
SHELF_MAX_BYTES = 500 * 1024 * 1024


class Shelf:
    def __init__(self, db: Database, live: Live, storage: Storage, blob_dir: Path):
        self.db, self.live, self.storage, self.blob_dir = db, live, storage, blob_dir
        self.fire = lambda event, data: None  # extensions' event hooks (set by the backend)
        live.getters["shelf_item"] = lambda i: self.item(int(i))

    # ── reading ────────────────────────────────────────────────────────────
    def _row(self, item_id: int) -> dict:
        row = self.db.q1("SELECT i.*, p.slug FROM shelf_items i JOIN projects p ON p.id=i.project_id WHERE i.id=? AND i.removed_at IS NULL", (item_id,))
        if not row:
            raise NotFound(f"shelf item {item_id}")
        return row

    def model(self, r: dict) -> s.ShelfItem:
        versions = self.db.q1("SELECT COUNT(*) AS n FROM shelf_versions WHERE item_id=?", (r["id"],))["n"]
        if r["mode"] == "snapshot":
            last = self.db.q1("SELECT size FROM shelf_versions WHERE item_id=? ORDER BY n DESC LIMIT 1", (r["id"],))
            size = last["size"] if last else None
        else:
            size = r["last_size"]
        return s.ShelfItem(id=r["id"], project=r["slug"], label=r["label"], volume=r["volume"], path=r["path"], mode=r["mode"], keep_versions=r["keep_versions"],
                           versions=versions, added_by=r["added_by"], run_id=r["run_id"], created_at=parse(r["created_at"]), updated_at=parse(r["updated_at"]),  # type: ignore[arg-type]
                           size=size, mime=guess_mime(r["path"]), missing=bool(r["missing"]) and r["mode"] == "live")

    def item(self, item_id: int) -> s.ShelfItem:
        return self.model(self._row(item_id))

    def items(self, project_id: int) -> list[s.ShelfItem]:
        rows = self.db.q("SELECT i.*, p.slug FROM shelf_items i JOIN projects p ON p.id=i.project_id WHERE i.project_id=? AND i.removed_at IS NULL ORDER BY i.updated_at DESC",
                         (project_id,))
        return [self.model(r) for r in rows]

    def versions(self, item_id: int) -> list[s.ShelfVersion]:
        self._row(item_id)
        return [s.ShelfVersion(n=r["n"], size=r["size"], run_id=r["run_id"], created_at=parse(r["created_at"]))  # type: ignore[arg-type]
                for r in self.db.q("SELECT * FROM shelf_versions WHERE item_id=? ORDER BY n DESC", (item_id,))]

    def raw(self, item_id: int, version: int | None) -> tuple[bytes, str, str]:
        """(bytes, mime, file name) of an item: a kept version, the snapshot, or the live file."""
        r = self._row(item_id)
        name = os.path.basename(r["path"])
        if version is None and r["mode"] == "live":
            return self.storage.read(r["volume"], r["path"], SHELF_MAX_BYTES), guess_mime(name), name
        q = "SELECT * FROM shelf_versions WHERE item_id=?" + (" AND n=?" if version is not None else " ORDER BY n DESC LIMIT 1")
        v = self.db.q1(q, (item_id, version) if version is not None else (item_id,))
        if not v:
            raise NotFound(f"version {version} of shelf item {item_id}")
        return Path(v["blob_path"]).read_bytes(), guess_mime(name), name

    # ── writing ────────────────────────────────────────────────────────────
    def add(self, project_id: int, volume: str, path: str, label: str | None, mode: str, keep: int, by: str, run_id: int | None) -> s.ShelfItem:
        info = self.storage.stat(volume, path)  # exists, is a regular file, no symlinks
        if mode == "snapshot" and info.st_size > SHELF_MAX_BYTES:
            raise BadRequest("too_large", "Too big to snapshot; shelve it live instead.")
        dup = self.db.q1("SELECT id FROM shelf_items WHERE project_id=? AND volume=? AND path=? AND mode=? AND removed_at IS NULL", (project_id, volume, path, mode))
        if dup and mode == "live":
            raise Conflict("already_shelved", f"/{volume}/{path} is already on this project's shelf (item {dup['id']}).")
        now = self.db.now()
        iid = self.db.ex(
            "INSERT INTO shelf_items(project_id, label, volume, path, mode, keep_versions, added_by, run_id, created_at, updated_at, last_mtime, last_size) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, (label or os.path.basename(path))[:200], volume, path, mode, keep if mode == "live" else 0, by, run_id, now, now, info.st_mtime, info.st_size),
        )
        if mode == "snapshot" or keep > 0:
            data = self.storage.read(volume, path, SHELF_MAX_BYTES)
            self._store_version(iid, data, run_id)
            self.db.ex("UPDATE shelf_items SET last_sha256=? WHERE id=?", (hashlib.sha256(data).hexdigest(), iid))
        elif info.st_size <= SHELF_MAX_BYTES:
            self.db.ex("UPDATE shelf_items SET last_sha256=? WHERE id=?", (hashlib.sha256(self.storage.read(volume, path)).hexdigest(), iid))
        self._changed(iid)
        slug = self.db.q1("SELECT slug FROM projects WHERE id=?", (project_id,))
        self.fire("file_shelved", {"shelf_item_id": iid, "project": slug["slug"] if slug else None, "volume": volume, "path": path})
        return self.item(iid)

    def remove(self, item_id: int, by: str) -> None:
        r = self._row(item_id)
        if by == "assistant" and r["added_by"] != "assistant":
            raise Conflict("not_yours", "The owner shelved this; only they can remove it.")
        self.db.ex("UPDATE shelf_items SET removed_at=? WHERE id=?", (self.db.now(), item_id))
        shutil.rmtree(self.blob_dir / str(item_id), ignore_errors=True)
        self.live.changed("shelf_item", item_id, deleted=True)

    def _store_version(self, item_id: int, data: bytes, run_id: int | None) -> None:
        n = (self.db.q1("SELECT MAX(n) AS n FROM shelf_versions WHERE item_id=?", (item_id,))["n"] or 0) + 1
        folder = self.blob_dir / str(item_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / str(n)
        path.write_bytes(data)
        self.db.ex("INSERT INTO shelf_versions(item_id, n, blob_path, size, sha256, run_id, created_at) VALUES(?,?,?,?,?,?,?)",
                   (item_id, n, str(path), len(data), hashlib.sha256(data).hexdigest(), run_id, self.db.now()))
        keep = self.db.q1("SELECT mode, keep_versions FROM shelf_items WHERE id=?", (item_id,))
        if keep and keep["mode"] == "live":
            for old in self.db.q("SELECT id, blob_path FROM shelf_versions WHERE item_id=? ORDER BY n DESC LIMIT -1 OFFSET ?", (item_id, keep["keep_versions"])):
                Path(old["blob_path"]).unlink(missing_ok=True)
                self.db.ex("DELETE FROM shelf_versions WHERE id=?", (old["id"],))

    def _changed(self, item_id: int) -> None:
        self.live.changed("shelf_item", item_id)

    # ── watching live items ────────────────────────────────────────────────
    def check(self, current_run: int | None = None) -> list[int]:
        """One pass over live items; returns the ids that changed."""
        changed = []
        for r in self.db.q("SELECT * FROM shelf_items WHERE mode='live' AND removed_at IS NULL"):
            try:
                info = self.storage.stat(r["volume"], r["path"])
            except (NotFound, BadRequest):
                if not r["missing"]:
                    self.db.ex("UPDATE shelf_items SET missing=1, updated_at=? WHERE id=?", (self.db.now(), r["id"]))
                    changed.append(r["id"])
                continue
            if not r["missing"] and info.st_mtime == r["last_mtime"] and info.st_size == r["last_size"]:
                continue
            sha = None
            if info.st_size <= SHELF_MAX_BYTES:
                data = self.storage.read(r["volume"], r["path"])
                sha = hashlib.sha256(data).hexdigest()
            self.db.ex("UPDATE shelf_items SET last_mtime=?, last_size=?, missing=0 WHERE id=?", (info.st_mtime, info.st_size, r["id"]))
            if sha is not None and sha == r["last_sha256"] and not r["missing"]:
                continue  # touched, same content
            self.db.ex("UPDATE shelf_items SET last_sha256=?, updated_at=? WHERE id=?", (sha, self.db.now(), r["id"]))
            if r["keep_versions"] > 0 and sha is not None:
                self._store_version(r["id"], data, current_run)
            changed.append(r["id"])
        for iid in changed:
            self._changed(iid)
        return changed

    async def watch(self, current_run) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await asyncio.to_thread(self.check, current_run())
            except Exception:  # noqa: BLE001
                log.exception("shelf check failed")
