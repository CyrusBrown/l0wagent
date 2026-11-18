"""State kept in one JSON file, so a restart keeps members. The CLI (`invite`) writes the same file
while the server runs, so every read checks the file's modification time first."""

from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from l0wproto import keys as lkeys
from l0wproto.times import now


def empty_state() -> dict:
    return {"members": {}, "invites": {}, "revocations": {}, "reregistrations": {}}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = empty_state()
        self._stamp: tuple | None = None
        self._refresh()

    def _stat(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    def _refresh(self) -> None:
        stamp = self._stat()
        if stamp == self._stamp and self._stamp is not None:
            return
        if stamp is None:
            self._state, self._stamp = empty_state(), None
            return
        self._state = {**empty_state(), **json.loads(self.path.read_text())}
        self._stamp = stamp

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._state, indent=1))
        os.replace(tmp, self.path)
        self._stamp = self._stat()

    @contextmanager
    def read(self):
        with self._lock:
            self._refresh()
            yield self._state

    @contextmanager
    def write(self):
        """Load the latest file, let the caller change it, save it (not saved if the caller raises)."""
        with self._lock:
            self._stamp = None
            self._refresh()
            yield self._state
            self._save()

    # --- convenience ---------------------------------------------------------------------

    def member(self, name: str) -> dict | None:
        with self.read() as s:
            m = s["members"].get(name)
            return json.loads(json.dumps(m)) if m else None

    def all_names(self) -> list[str]:
        with self.read() as s:
            return list(s["members"])

    def make_invite(self, *, days: int = 7, name_hint: str | None = None, reregister: str | None = None) -> str:
        code = secrets.token_urlsafe(24)  # 192 bits, URL-safe alphabet
        with self.write() as s:
            s["invites"][code] = {"expires": now() + days * 86400, "used_by": None, "name_hint": name_hint,
                                  "reregister": reregister}
        return code


def load_hub_key(seed_path: Path) -> lkeys.KeyPair:
    """A fixed hub key: the seed file is created on first start and reused after."""
    seed_path = Path(seed_path)
    if seed_path.exists():
        seed = bytes.fromhex(seed_path.read_text().strip())
    else:
        seed = os.urandom(32)
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(seed.hex() + "\n")
        seed_path.chmod(0o600)
    # a fixed `created` so the key object (and its kid) is identical across restarts
    return lkeys.from_seed(seed, "sign", "2026-11-17T00:00:00Z")
