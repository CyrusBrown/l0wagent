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

    def peers(self) -> dict:
        """peers.json: friends by lowercase address (see peers.py for the record)."""
        return _read(self.dir / "peers.json", {})  # type: ignore[return-value]

    def save_peers(self, peers: dict) -> None:
        _write(self.dir / "peers.json", peers)

    def outbox(self) -> list:
        """outbox.json: the envelopes waiting to be delivered, and the recently finished ones."""
        return _read(self.dir / "outbox.json", [])  # type: ignore[return-value]

    def save_outbox(self, entries: list) -> None:
        _write(self.dir / "outbox.json", entries)

    def hubs(self) -> dict:
        """hubs.json: the hub signing key pinned on first contact, by hub domain."""
        return _read(self.dir / "hubs.json", {})  # type: ignore[return-value]

    def save_hubs(self, hubs: dict) -> None:
        _write(self.dir / "hubs.json", hubs)

    def services(self) -> list:
        """services.json: the addresses of the hub's services, as control last told us."""
        return _read(self.dir / "services.json", [])  # type: ignore[return-value]

    def save_services(self, addresses: list) -> None:
        _write(self.dir / "services.json", addresses)
