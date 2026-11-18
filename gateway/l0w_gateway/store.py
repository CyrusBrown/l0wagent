"""The gateway's state: files in the data dir, written atomically with mode 0600."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


def _write(path: Path, obj: object) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class Store:
    """keys.json: {"sign": {"seed", "pub"}, "seal": {"seed", "pub"}, "recover": {"pub"}, "retired": [pub...], "history": [doc...]}
    or None before the first registration. tunnel.json: the tunnel config or null. tunnel-status.json: the last status or null."""

    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def keys(self) -> dict | None:
        return _read(self.dir / "keys.json", None)  # type: ignore[return-value]

    def save_keys(self, data: dict) -> None:
        _write(self.dir / "keys.json", data)

    def tunnel(self) -> dict | None:
        return _read(self.dir / "tunnel.json", None)  # type: ignore[return-value]

    def save_tunnel(self, config: dict | None) -> None:
        _write(self.dir / "tunnel.json", config)

    def status(self) -> dict | None:
        return _read(self.dir / "tunnel-status.json", None)  # type: ignore[return-value]

    def save_status(self, status: dict | None) -> None:
        _write(self.dir / "tunnel-status.json", status)
