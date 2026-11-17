"""Reading the agent's storage (/workspace and /work), safely.

The agent controls these trees, so every path is resolved one component at a time with
O_NOFOLLOW from the volume's root: a symlink anywhere in the path is refused rather than
followed (it could point at control's own files). Special files (FIFOs, devices) are
refused too. Control only reads here; its one write path is uploads (backends/sqlite.py).

Git history comes from dulwich, a pure-Python reader: control never runs `git` on the
agent's repos, whose config could otherwise make git execute commands. `.git` and its
`objects` folder must be real folders, not symlinks.
"""

from __future__ import annotations

import difflib
import mimetypes
import os
import re
import stat as st
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .backends.base import BadRequest, NotFound

O_DIR = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
MAX_ENTRIES = 5000
ZIP_MAX_BYTES = 1024 * 1024 * 1024
ZIP_MAX_FILES = 50_000
CHUNK = 1 << 20
REFS_MAX = 20_000
PATCH_MAX = 2 * 1024 * 1024
RUN_RE = re.compile(r"\brun #(\d+)")

EXTRA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".ipynb": "application/x-ipynb+json",
    ".stl": "model/stl", ".3mf": "model/3mf", ".step": "model/step", ".stp": "model/step", ".toml": "text/plain", ".yaml": "text/yaml",
    ".yml": "text/yaml", ".py": "text/x-python", ".ts": "text/plain", ".tsx": "text/plain", ".rs": "text/plain", ".go": "text/plain",
    ".sh": "text/x-sh", ".log": "text/plain", ".ini": "text/plain", ".cfg": "text/plain", ".env": "text/plain", ".jsonl": "application/x-ndjson",
}


def guess_mime(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return EXTRA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def clean(rel: str) -> list[str]:
    """Split a relative path into safe components."""
    if "\x00" in rel:
        raise BadRequest("bad_path", "Invalid path.")
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise BadRequest("bad_path", "Paths can't go up ('..').")
    return parts


@dataclass
class Entry:
    name: str
    dir: bool
    symlink: bool
    size: int | None
    mtime: float | None


class Storage:
    def __init__(self, roots: dict[str, Path]):
        self.roots = roots
        self._run_maps: dict[tuple[str, bytes], dict[bytes, tuple[int | None, int]]] = {}

    # ── safe opening ───────────────────────────────────────────────────────
    def _root(self, volume: str) -> int:
        if volume not in self.roots:
            raise BadRequest("bad_volume", f"Unknown volume {volume}.")
        try:
            return os.open(self.roots[volume], os.O_RDONLY | O_DIR)
        except OSError as e:
            raise NotFound(f"/{volume} isn't available here ({e.strerror})") from e

    def open(self, volume: str, rel: str, want_dir: bool = False) -> int:
        """An fd for volume/rel, refusing symlinks and special files at every step."""
        parts = clean(rel)
        fd = self._root(volume)
        try:
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                as_dir = not last or want_dir
                flags = os.O_RDONLY | O_NOFOLLOW | (O_DIR if as_dir else O_NONBLOCK)
                try:
                    nfd = os.open(part, flags, dir_fd=fd)
                except OSError as e:
                    try:
                        info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        raise NotFound(f"/{volume}/{'/'.join(parts[: i + 1])}") from e
                    if st.S_ISLNK(info.st_mode):
                        raise BadRequest("symlink", f"/{volume}/{'/'.join(parts[: i + 1])} is a symlink; links aren't followed.") from e
                    if as_dir and not st.S_ISDIR(info.st_mode):
                        raise BadRequest("not_a_folder", f"/{volume}/{'/'.join(parts[: i + 1])} isn't a folder.") from e
                    raise NotFound(f"/{volume}/{'/'.join(parts[: i + 1])}") from e
                os.close(fd)
                fd = nfd
            info = os.fstat(fd)
            if want_dir and not st.S_ISDIR(info.st_mode):
                raise BadRequest("not_a_folder", f"/{volume}/{rel} isn't a folder.")
            if not want_dir and not st.S_ISREG(info.st_mode):
                raise BadRequest("not_a_file", f"/{volume}/{rel} isn't a regular file.")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def is_dir(self, volume: str, rel: str) -> bool:
        try:
            os.close(self.open(volume, rel, want_dir=True))
            return True
        except (BadRequest, NotFound):
            return False

    # ── listing and reading ────────────────────────────────────────────────
    def list(self, volume: str, rel: str) -> list[Entry]:
        fd = self.open(volume, rel, want_dir=True)
        try:
            out = []
            for name in sorted(os.listdir(fd))[:MAX_ENTRIES]:
                if name == ".git":
                    continue
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                is_dir = st.S_ISDIR(info.st_mode)
                out.append(Entry(name, is_dir, st.S_ISLNK(info.st_mode), None if is_dir else info.st_size, info.st_mtime))
            return sorted(out, key=lambda e: (not e.dir, e.name.lower()))
        finally:
            os.close(fd)

    def stat(self, volume: str, rel: str) -> os.stat_result:
        fd = self.open(volume, rel)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def read(self, volume: str, rel: str, limit: int | None = None) -> bytes:
        fd = self.open(volume, rel)
        try:
            size = os.fstat(fd).st_size
            if limit is not None and size > limit:
                raise BadRequest("too_large", f"The file is {size // 1024 // 1024} MB; download it instead.")
            chunks = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_range(self, volume: str, rel: str, start: int, end: int) -> tuple[bytes, int]:
        """Bytes [start, end] (inclusive) and the file's size."""
        fd = self.open(volume, rel)
        try:
            size = os.fstat(fd).st_size
            end = min(end, size - 1)
            return (os.pread(fd, end - start + 1, start) if size and start <= end else b""), size
        finally:
            os.close(fd)

    def iter_file(self, volume: str, rel: str) -> tuple[Iterator[bytes], int]:
        fd = self.open(volume, rel)
        size = os.fstat(fd).st_size

        def gen() -> Iterator[bytes]:
            try:
                while chunk := os.read(fd, CHUNK):
                    yield chunk
            finally:
                os.close(fd)

        return gen(), size

    def zip(self, volume: str, rel: str) -> tempfile.SpooledTemporaryFile:
        """A zip of a folder (symlinks and special files skipped), spooled to disk if big."""
        out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        total = files = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            def walk(prefix: list[str]) -> None:
                nonlocal total, files
                for e in self.list(volume, "/".join(prefix)):
                    if e.symlink:
                        continue
                    sub = [*prefix, e.name]
                    if e.dir:
                        walk(sub)
                        continue
                    files += 1
                    total += e.size or 0
                    if files > ZIP_MAX_FILES or total > ZIP_MAX_BYTES:
                        raise BadRequest("too_large", "That folder is too big to zip here.")
                    try:
                        fd = self.open(volume, "/".join(sub))
                    except (BadRequest, NotFound):
                        continue
                    # Stream the file in chunks: the agent decides how big its files are.
                    try:
                        info = zipfile.ZipInfo("/".join(sub[len(base):]) or e.name, datetime.fromtimestamp(e.mtime or 0, UTC).timetuple()[:6])
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o644 << 16
                        with z.open(info, "w", force_zip64=True) as dest:
                            while chunk := os.read(fd, CHUNK):
                                dest.write(chunk)
                    finally:
                        os.close(fd)

            base = clean(rel)
            walk(base)
        out.seek(0)
        return out

    # ── git ────────────────────────────────────────────────────────────────
    def repo_for(self, volume: str, rel: str):
        """The repo containing volume/rel (the deepest folder with a real .git), and the
        path inside it, or None."""
        from dulwich.repo import Repo

        parts = clean(rel)
        for depth in range(len(parts), -1, -1):
            folder = "/".join(parts[:depth])
            if not self._real_git(volume, folder):
                continue
            try:
                repo = Repo(str(self.roots[volume] / folder))
            except Exception:  # noqa: BLE001 - not a usable repo
                continue
            return repo, "/".join(parts[depth:])
        return None

    def _real_git(self, volume: str, folder: str) -> bool:
        try:
            fd = self.open(volume, folder, want_dir=True)
        except (BadRequest, NotFound):
            return False
        try:
            for name in (".git", ".git/objects", ".git/refs"):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not st.S_ISDIR(info.st_mode):
                    return False
            # dulwich opens the rest by path and follows links, so refuse any link in what it reads,
            # and the files that point outside the repo.
            for name in (".git/HEAD", ".git/packed-refs", ".git/config"):
                try:
                    if st.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        return False
                except FileNotFoundError:
                    pass
            for name in (".git/commondir", ".git/objects/info/alternates"):
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                    return False
                except FileNotFoundError:
                    pass
            return self._no_links(os.open(".git/refs", O_DIR | O_NOFOLLOW | os.O_RDONLY, dir_fd=fd), [0])
        except OSError:
            return False
        finally:
            os.close(fd)

    def _no_links(self, fd: int, seen: list[int]) -> bool:
        """True when nothing under this folder (closed here) is a symlink."""
        try:
            with os.scandir(fd) as it:
                for e in it:
                    seen[0] += 1
                    if seen[0] > REFS_MAX or e.is_symlink():
                        return False
                    if e.is_dir(follow_symlinks=False):
                        sub = os.open(e.name, O_DIR | O_NOFOLLOW | os.O_RDONLY, dir_fd=fd)
                        if not self._no_links(sub, seen):
                            return False
            return True
        finally:
            os.close(fd)

    def branch(self, repo) -> str | None:
        try:
            ref = repo.refs.read_ref(b"HEAD") or b""
            return ref.decode().removeprefix("ref: refs/heads/") if ref.startswith(b"ref: ") else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _blob(repo, commit, path: bytes) -> bytes | None:
        from dulwich.object_store import tree_lookup_path

        try:
            _, sha = tree_lookup_path(repo.__getitem__, commit.tree, path)
            obj = repo[sha]
            return obj.data if obj.type_name == b"blob" else None
        except KeyError:
            return None

    def history(self, volume: str, rel: str, limit: int = 30) -> list[dict]:
        found = self.repo_for(volume, rel)
        if not found:
            return []
        repo, inner = found
        try:
            walker = repo.get_walker(paths=[inner.encode()], max_entries=limit)
            out = []
            for entry in walker:
                c = entry.commit
                parent = repo[c.parents[0]] if c.parents else None
                new = self._blob(repo, c, inner.encode())
                old = self._blob(repo, parent, inner.encode()) if parent else None
                added, removed = _count(old, new)
                msg = c.message.decode(errors="replace").strip()
                m = RUN_RE.search(msg)
                out.append({"rev": c.id.decode(), "at": datetime.fromtimestamp(c.commit_time, UTC), "message": msg.splitlines()[0] if msg else "",
                            "added": added, "removed": removed, "run_id": int(m.group(1)) if m else None, "time": c.commit_time})
            return out
        except KeyError:
            return []
        finally:
            repo.close()

    def diff(self, volume: str, rel: str, rev: str) -> dict:
        found = self.repo_for(volume, rel)
        if not found:
            raise NotFound(f"no git history for /{volume}/{rel}")
        repo, inner = found
        try:
            try:
                c = repo[rev.encode()]
            except (KeyError, ValueError):
                raise NotFound(f"revision {rev}") from None
            if c.type_name != b"commit":
                raise NotFound(f"revision {rev}")
            parent = repo[c.parents[0]] if c.parents else None
            new = self._blob(repo, c, inner.encode())
            old = self._blob(repo, parent, inner.encode()) if parent else None
            msg = c.message.decode(errors="replace").strip()
            m = RUN_RE.search(msg)
            if (old and b"\0" in old[:8000]) or (new and b"\0" in new[:8000]):
                patch = "Binary file changed."
            else:
                a = (old or b"").decode(errors="replace").splitlines(keepends=True)
                b = (new or b"").decode(errors="replace").splitlines(keepends=True)
                patch = "".join(difflib.unified_diff(a, b, f"a/{inner}", f"b/{inner}"))[:PATCH_MAX] or "No changes to this file."
            added, removed = _count(old, new)
            return {"rev": c.id.decode(), "parent": parent.id.decode() if parent else None, "at": datetime.fromtimestamp(c.commit_time, UTC),
                    "message": msg, "patch": patch, "added": added, "removed": removed, "run_id": int(m.group(1)) if m else None, "time": c.commit_time}
        finally:
            repo.close()

    def last_changes(self, volume: str, rel_dir: str, max_commits: int = 300) -> dict[str, tuple[int | None, int]]:
        """For the files under a folder: (run id from the commit message, commit time) of the
        last commit that changed each one. Cached per repo HEAD."""
        found = self.repo_for(volume, rel_dir)
        if not found:
            return {}
        repo, inner = found
        try:
            try:
                head = repo.head()
            except KeyError:
                return {}
            key = (str(repo.path), head)
            if key not in self._run_maps:
                seen: dict[bytes, tuple[int | None, int]] = {}
                for entry in repo.get_walker(max_entries=max_commits):
                    m = RUN_RE.search(entry.commit.message.decode(errors="replace"))
                    info = (int(m.group(1)) if m else None, entry.commit.commit_time)
                    changes = entry.changes()
                    flat = [c for group in changes for c in (group if isinstance(group, list) else [group])]
                    for ch in flat:
                        for side in (ch.new, ch.old):
                            if side and side.path and side.path not in seen:
                                seen[side.path] = info
                self._run_maps = {k: v for k, v in self._run_maps.items() if k[0] != key[0]}
                self._run_maps[key] = seen
            prefix = (inner + "/") if inner else ""
            return {p.decode(errors="replace")[len(prefix):]: v for p, v in self._run_maps[key].items() if p.decode(errors="replace").startswith(prefix)}
        finally:
            repo.close()


def _count(old: bytes | None, new: bytes | None) -> tuple[int, int]:
    if (old and b"\0" in old[:8000]) or (new and b"\0" in new[:8000]):
        return 0, 0
    a = (old or b"").decode(errors="replace").splitlines()
    b = (new or b"").decode(errors="replace").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed
