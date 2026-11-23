"""The `[extension.NAME]` entries ops reads and writes.

Two files name git sources, and `./l0w extensions sync` and control read both:
- `extensions.toml` next to compose.yaml: the owner's own file. Ops only reads it (it is mounted read-only).
- `extensions/.ops.toml`: written by ops when the owner installs from a git URL in the web UI. The owner's action is the
  authority; the file is the record that `./l0w extensions sync` and a restore from backup use.

Edits are textual (other entries and comments are kept), written atomically, and checked by parsing the result.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from .common import OpsError

HEADER = ("# Written by the ops container: the extensions installed from the web UI (Settings -> Extensions).\n"
          "# ./l0w extensions sync reads this file as well as extensions.toml. Edit extensions.toml for your own entries.\n")
_TABLE = re.compile(r"^\s*\[")


def _header_re(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(rf'^\s*\[\s*extension\s*\.\s*(?:"{n}"|{n})\s*\]\s*(?:#.*)?$')


def read_entries(path: Path) -> dict[str, dict]:
    """The entries in a file; a missing, unreadable or invalid file has none."""
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return {n: v for n, v in (raw.get("extension") or {}).items() if isinstance(v, dict)}


def lookup(name: str, *files: Path) -> dict | None:
    """The first file's entry for `name` (the owner's file is listed before .ops.toml, so it wins)."""
    for f in files:
        e = read_entries(f).get(name)
        if e is not None:
            return e
    return None


def _block(name: str, git: str, commit: str, path: str | None) -> list[str]:
    lines = [f"[extension.{name}]", f"git = {json.dumps(git)}", f"commit = {json.dumps(commit)}"]
    if path:
        lines.append(f"path = {json.dumps(path)}")
    return lines


def _split(text: str, name: str) -> tuple[list[str], list[str], list[str]] | None:
    """(before, block, after) around the entry's table, or None when there isn't one."""
    lines = text.splitlines()
    hdr = _header_re(name)
    for i, line in enumerate(lines):
        if hdr.match(line):
            j = i + 1
            while j < len(lines) and not _TABLE.match(lines[j]):
                j += 1
            return lines[:i], lines[i:j], lines[j:]
    return None


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def set_entry(path: Path, name: str, git: str, commit: str, sub: str | None) -> str | None:
    """Write or replace [extension.NAME]. Returns the file's previous text (None if it didn't exist) so a failed install can restore it."""
    try:
        old = path.read_text()
    except FileNotFoundError:
        old = None
    except (OSError, UnicodeDecodeError) as e:
        raise OpsError(500, "toml_unreadable", f"{path.name}: {e}") from e
    text = HEADER if old is None else old
    block = _block(name, git, commit, sub)
    parts = _split(text, name)
    if parts:
        before, _old, after = parts
        trailing = [""] if after else []
        new_lines = before + block + trailing + after
    else:
        new_lines = text.splitlines()
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines += block
    new = "\n".join(new_lines) + "\n"
    _check(new, name, {"git": git, "commit": commit, **({"path": sub} if sub else {})})
    write_atomic(path, new)
    return old


def remove_entry(path: Path, name: str) -> bool:
    """Drop [extension.NAME] (its table only). True if there was one."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    parts = _split(text, name)
    if not parts:
        return False
    before, _block_lines, after = parts
    while before and not before[-1].strip() and (not after or after[0].strip() == ""):
        before.pop()
    new = "\n".join(before + ([""] if before and after else []) + after).rstrip("\n") + "\n"
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise OpsError(500, "toml_invalid", f"removing {name} would leave {path.name} invalid: {e}") from e
    write_atomic(path, new)
    return True


def restore(path: Path, old: str | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
    else:
        write_atomic(path, old)


def _check(text: str, name: str, want: dict) -> None:
    try:
        got = (tomllib.loads(text).get("extension") or {}).get(name)
    except tomllib.TOMLDecodeError as e:
        raise OpsError(500, "toml_invalid", f".ops.toml would not be valid TOML: {e}") from e
    if got != want:
        raise OpsError(500, "toml_invalid", f".ops.toml: couldn't write the [extension.{name}] entry safely; edit the file by hand")
