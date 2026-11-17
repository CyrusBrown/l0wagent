"""Disk and volume sizes for Settings → System.

Walking a big /work tree is slow, so the sizes are measured in a background housekeeping step at most every
10 minutes (with a time cap) and the status request only reads the cache. Free space is one statvfs call.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from .api import schemas as s

REFRESH_SECONDS = 600
WALK_BUDGET_SECONDS = 60
GB = 1024**3
# Only the volumes control mounts are measured. /cache isn't mounted into control and the runner reports no sizes.
UNMOUNTED = {"cache": "Not visible to control (it belongs to the agent computer)."}


def tree_bytes(root: Path, deadline: float) -> tuple[int, bool]:
    """(bytes, complete) for a directory tree. Doesn't follow symlinks; stops at the deadline."""
    total = 0
    stack = [str(root)]
    while stack:
        if time.monotonic() > deadline:
            return total, False
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


class DiskUsage:
    def __init__(self, data_dir: Path, db_path: str, volumes: dict[str, Path]):
        self.data_dir = data_dir
        self.db_path = db_path
        self.volumes = volumes
        self.measured_at: datetime | None = None
        self.last_run = 0.0
        self._sizes: list[s.VolumeUsage] = []

    def refresh_due(self) -> bool:
        return not self.last_run or time.monotonic() - self.last_run >= REFRESH_SECONDS

    def refresh(self) -> None:
        """Blocking: call from a thread."""
        self.last_run = time.monotonic()
        deadline = time.monotonic() + WALK_BUDGET_SECONDS
        out = []
        for name, path in self.volumes.items():
            if not path.is_dir():
                out.append(s.VolumeUsage(name=name, note="Not mounted."))
                continue
            size, complete = tree_bytes(path, deadline)
            out.append(s.VolumeUsage(name=name, bytes=size, note=None if complete else "At least this much: measuring stopped after a minute."))
        for name, note in UNMOUNTED.items():
            out.append(s.VolumeUsage(name=name, note=note))
        self._sizes = out
        self.measured_at = datetime.now(UTC)

    def db_bytes(self) -> int | None:
        total, seen = 0, False
        for suffix in ("", "-wal"):
            try:
                total += os.stat(self.db_path + suffix).st_size
                seen = True
            except OSError:
                pass
        return total if seen else None

    def free(self) -> tuple[int, int]:
        du = shutil.disk_usage(self.data_dir)
        return du.free, du.total

    def status(self, warn_free_gb: float) -> s.StorageStatus:
        free, total = self.free()
        return s.StorageStatus(disk_free_bytes=free, disk_total_bytes=total, warn_free_gb=warn_free_gb, low=free < warn_free_gb * GB,
                               db_bytes=self.db_bytes(), volumes=list(self._sizes), measured_at=self.measured_at)
