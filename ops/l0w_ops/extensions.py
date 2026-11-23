"""Install and remove extensions: the checks, then the git work (see gitx.py), then an atomic swap into extensions/NAME."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import tomllib
from pathlib import Path

from l0wproto import payloads
from l0wproto.errors import ProtocolError
from l0wproto.keys import Key

from . import gitx, tomlfile
from .common import EXT_NAME, OpsError
from .config import Settings

SOURCE_FILE = ".l0w-source.json"
SOURCES = ("owner", "toml", "hub")


def check_name(name: object) -> str:
    if not isinstance(name, str) or not EXT_NAME.match(name):
        raise OpsError(400, "bad_name", "name must be 2-31 lowercase letters, digits or dashes, starting with a letter")
    return name


def _dir(s: Settings, name: str) -> Path:
    return s.extensions_dir / name


def read_source(d: Path) -> dict | None:
    """The record ops left in an extension it installed; None for anything else (an owner's folder, a git clone)."""
    f = d / SOURCE_FILE
    try:
        if d.is_symlink() or not d.is_dir() or f.is_symlink() or not f.is_file():
            return None
        v = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def installed(s: Settings) -> list[dict]:
    out = []
    try:
        names = sorted(p.name for p in s.extensions_dir.iterdir())
    except OSError:
        return out
    for n in names:
        if n.startswith(".") or not EXT_NAME.match(n):
            continue
        src = read_source(_dir(s, n))
        if src:
            out.append({"name": n, **{k: src.get(k) for k in ("git", "commit", "path", "source", "installed_at")}})
    return out


def resolve(s: Settings, git: object, ref: object) -> dict:
    url = gitx.check_url(git, s)
    return {"commit": gitx.resolve_ref(s, url, gitx.check_ref(ref))}


def _check_hub(s: Settings, doc: object, hub_key: object, url: str, commit: str, sub: str | None) -> None:
    if not isinstance(doc, dict) or not isinstance(hub_key, dict):
        raise OpsError(400, "bad_request", "a hub install needs service_doc and hub_key")
    try:
        key = Key.from_dict(hub_key)
        if key.use != "sign" or key.alg != "Ed25519":
            raise OpsError(403, "bad_service_doc", "hub_key must be the hub's Ed25519 signing key")
        payload = payloads.verify_service(doc, key)
    except (ProtocolError, ValueError, KeyError, TypeError) as e:
        raise OpsError(403, "bad_service_doc", f"the service document isn't signed by that hub key: {e}") from e
    ref = payload.get("extension")
    if not isinstance(ref, dict):
        raise OpsError(403, "no_extension", "the service document names no extension")
    if ref.get("git") != url or ref.get("commit") != commit or gitx.check_subpath(ref.get("path")) != sub:
        raise OpsError(403, "doc_mismatch", "git, commit and path must be exactly what the hub's service document lists")


def _check_toml(s: Settings, name: str, url: str, commit: str, sub: str | None) -> None:
    entry = tomlfile.lookup(name, s.toml_file, s.ops_toml)
    if entry is None:
        raise OpsError(403, "not_in_toml", f"extensions.toml has no [extension.{name}] entry (or ops can't see it: restart the ops container after replacing the file)")
    if entry.get("git") != url or entry.get("commit") != commit or (entry.get("path") or None) != sub:
        raise OpsError(403, "toml_mismatch", f"git, commit and path must be exactly what [extension.{name}] says")


def install(s: Settings, body: dict) -> dict:
    name = check_name(body.get("name"))
    url = gitx.check_url(body.get("git"), s)
    commit = gitx.check_commit(body.get("commit"))
    sub = gitx.check_subpath(body.get("path"))
    source = body.get("source")
    if source not in SOURCES:
        raise OpsError(400, "bad_source", "source must be owner, toml or hub")
    if source == "hub":
        _check_hub(s, body.get("service_doc"), body.get("hub_key"), url, commit, sub)
    elif source == "toml":
        _check_toml(s, name, url, commit, sub)

    target = _dir(s, name)
    replacing = target.exists() or target.is_symlink()
    if replacing and read_source(target) is None:
        raise OpsError(409, "not_ours", f"extensions/{name} wasn't installed by ops (it is yours, or `./l0w extensions sync` made it); move it aside first")

    s.extensions_dir.mkdir(parents=True, exist_ok=True)
    gd = gitx.fetch_commit(s, url, commit)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=s.extensions_dir))
    old_text: str | None = None
    wrote_toml = False
    try:
        tree = tmp / "tree"
        tree.mkdir()
        files = gitx.archive_tree(s, gd, commit, sub, tree)
        _check_manifest(tree, name)
        record = {"git": url, "commit": commit, "path": sub, "source": source, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        (tree / SOURCE_FILE).unlink(missing_ok=True)
        (tree / SOURCE_FILE).write_text(json.dumps(record, indent=2) + "\n")
        os.chmod(tree, 0o755)
        if source == "owner":  # the owner's action is the authority: record it first, in the file `./l0w extensions sync` reads
            old_text = tomlfile.set_entry(s.ops_toml, name, url, commit, sub)
            wrote_toml = True
        _swap(s, tree, target, replacing)
    except BaseException:
        if wrote_toml:
            tomlfile.restore(s.ops_toml, old_text)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"name": name, "commit": commit, "path": sub, "source": source, "files": files, "replaced": replacing}


def _check_manifest(tree: Path, name: str) -> None:
    try:
        raw = tomllib.loads((tree / "extension.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise OpsError(422, "no_manifest", f"the repository (or path) has no valid extension.toml at its top: {e}") from e
    if raw.get("name") != name:
        raise OpsError(422, "name_mismatch", f"extension.toml names {raw.get('name')!r}, not {name!r}")


def _swap(s: Settings, new: Path, target: Path, replacing: bool) -> None:
    """Put `new` at `target` in two renames: the old one is moved aside first and restored if the second rename fails."""
    aside = None
    if replacing:
        aside = Path(tempfile.mkdtemp(prefix=".old-", dir=s.extensions_dir))
        aside_target = aside / "old"
        os.rename(target, aside_target)
    try:
        os.rename(new, target)
    except OSError as e:
        if aside is not None:
            os.rename(aside / "old", target)
            shutil.rmtree(aside, ignore_errors=True)
        raise OpsError(500, "swap_failed", f"couldn't put the new extension in place: {e}") from e
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)


def remove(s: Settings, body: dict) -> dict:
    name = check_name(body.get("name"))
    target = _dir(s, name)
    src = read_source(target)
    if src is None:
        raise OpsError(409, "not_ours", f"extensions/{name} wasn't installed by ops; ops only removes what it installed")
    aside = Path(tempfile.mkdtemp(prefix=".old-", dir=s.extensions_dir))
    os.rename(target, aside / "old")
    shutil.rmtree(aside, ignore_errors=True)
    entry_removed = False
    left_in_toml = False
    if src.get("source") in ("owner", "toml"):
        entry_removed = tomlfile.remove_entry(s.ops_toml, name)
        left_in_toml = tomlfile.lookup(name, s.toml_file) is not None  # the owner's own file is read-only here
    return {"name": name, "removed": True, "toml_entry_removed": entry_removed, "still_in_extensions_toml": left_in_toml}
