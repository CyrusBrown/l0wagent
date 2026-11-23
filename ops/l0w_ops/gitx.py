"""Git work for ops: resolve a ref to a commit, fetch a commit into a bare cache, and extract its tree safely.

Nothing here runs code from the repository: only `ls-remote`, `fetch`, `rev-parse` and `archive` run (no checkout, so no hooks
or filters), the archive is read with Python's tarfile and only regular files and directories are written; symlinks, hard
links, devices, absolute names and `..` are refused, and the size and number of files are capped.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import stat
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

from .common import COMMIT_RE, OpsError
from .config import Settings

REF_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}\Z")
SUBPATH_BAD = re.compile(r"[\x00-\x1f\\:]")


def _resolve_host(host: str, port: int) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        return []


resolve = _resolve_host  # tests replace this


def _private(text: str) -> bool:
    a = ipaddress.ip_address(text.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not a.is_global or a.is_multicast


def check_url(url: object, s: Settings) -> str:
    """An https git URL without credentials that names a public host (unless the operator allows private ones)."""
    if not isinstance(url, str) or not url or len(url) > 500 or url.startswith("-") or re.search(r"[\x00-\x20\x7f]", url):
        raise OpsError(400, "bad_git_url", "git must be an https URL")
    sp = urlsplit(url)
    if sp.scheme == "file" and s.allow_file_git:
        return url
    if sp.scheme != "https" or not sp.hostname:
        raise OpsError(400, "bad_git_url", "git must be an https URL")
    if sp.username is not None or sp.password is not None:
        raise OpsError(400, "bad_git_url", "git URLs can't carry a user name or password")
    try:
        port = sp.port or 443
    except ValueError as e:
        raise OpsError(400, "bad_git_url", "bad port in the git URL") from e
    if not s.allow_private_hosts:
        addrs = resolve(sp.hostname, port)
        if not addrs:
            raise OpsError(400, "bad_git_url", f"can't resolve {sp.hostname}")
        if any(_private(a) for a in addrs):
            raise OpsError(400, "bad_git_url", f"{sp.hostname} isn't a public address")
    return url


def check_commit(commit: object) -> str:
    if not isinstance(commit, str) or not COMMIT_RE.match(commit):
        raise OpsError(400, "bad_commit", "commit must be the full 40 (or 64) lowercase hex digits")
    return commit


def check_subpath(path: object) -> str | None:
    if path is None or path in ("", "."):  # "." is how a service document says "the repository's top"
        return None
    if not isinstance(path, str) or path.startswith(("/", "-")) or SUBPATH_BAD.search(path) or len(path) > 300 \
            or any(seg in ("", ".", "..") for seg in path.split("/")):
        raise OpsError(400, "bad_path", "path must be a relative folder inside the repository, without '..'")
    return path


def check_ref(ref: object) -> str:
    if ref is None or ref == "":
        return "HEAD"
    if not isinstance(ref, str) or not REF_RE.match(ref) or ref.startswith(("-", "/")) or ".." in ref:
        raise OpsError(400, "bad_ref", "ref must be a branch, tag or HEAD")
    return ref


def _env(s: Settings) -> dict[str, str]:
    allowed = "https:file" if s.allow_file_git else "https"
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(s.home), "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": allowed, "GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "60",
        **s.git_env,
    }


def run_git(s: Settings, args: list[str], *, git_dir: Path | None = None, timeout: int | None = None) -> str:
    cmd = ["git"] + (["--git-dir", str(git_dir)] if git_dir else []) + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_env(s), timeout=timeout or s.limits["git_seconds"])
    except subprocess.TimeoutExpired as e:
        raise OpsError(504, "git_timeout", f"git {args[0]} took too long") from e
    except OSError as e:
        raise OpsError(500, "git_missing", f"couldn't run git: {e}") from e
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["git failed"])[-1][:300]
        raise OpsError(502, "git_failed", f"git {args[0]}: {last}")
    return r.stdout


def resolve_ref(s: Settings, url: str, ref: str) -> str:
    """The commit a ref points at right now (an annotated tag resolves to the commit it names)."""
    out = run_git(s, ["ls-remote", "--", url, ref, ref + "^{}"] if ref != "HEAD" else ["ls-remote", "--", url, "HEAD"])
    found: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if COMMIT_RE.match(sha):
            found[name] = sha
    order = ["HEAD"] if ref == "HEAD" else [f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref, ref + "^{}"]
    for name in order:
        if name in found:
            return found[name]
    raise OpsError(404, "ref_not_found", f"{ref} isn't in that repository")


def cache_dir(s: Settings, url: str) -> Path:
    return s.extensions_dir / ".cache" / (hashlib.sha256(url.encode()).hexdigest() + ".git")


def fetch_commit(s: Settings, url: str, commit: str) -> Path:
    """Make sure `commit` is in this URL's bare cache; returns the cache. The commit must really be that commit."""
    gd = cache_dir(s, url)
    gd.parent.mkdir(parents=True, exist_ok=True)
    if not (gd / "HEAD").exists():
        run_git(s, ["init", "--bare", "--quiet", str(gd)])
    try:
        run_git(s, ["fetch", "--quiet", "--no-tags", "--depth", "1", "--", url, commit], git_dir=gd)
    except OpsError as first:
        if first.code == "git_timeout":
            raise
        # A server that won't serve a commit by id: fetch its branches and tags and look for it there.
        run_git(s, ["fetch", "--quiet", "--force", "--tags", "--", url, "+refs/heads/*:refs/remotes/origin/*"], git_dir=gd)
    try:
        full = run_git(s, ["rev-parse", "--verify", "--quiet", "--end-of-options", commit + "^{commit}"], git_dir=gd).strip()
    except OpsError as e:
        raise OpsError(422, "commit_not_found", f"{commit[:12]} isn't in that repository") from e
    if full != commit:
        raise OpsError(422, "commit_mismatch", "git didn't resolve the commit to itself; use the full commit id")
    return gd


def archive_tree(s: Settings, gd: Path, commit: str, sub: str | None, dest: Path) -> int:
    """`git archive` of the commit (or of `sub` inside it) extracted into the empty folder `dest`. Returns the number of files."""
    spec = f"{commit}:{sub}" if sub else commit
    cmd = ["git", "--git-dir", str(gd), "archive", "--format=tar", spec]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=_env(s))
    except OSError as e:
        raise OpsError(500, "git_missing", f"couldn't run git: {e}") from e
    ok = False
    try:
        try:
            count = extract_tar(proc.stdout, dest, max_bytes=s.limits["bytes"], max_files=s.limits["files"])
            while proc.stdout.read(1 << 20):  # git pads the archive; let it finish writing
                pass
            tar_error = None
        except tarfile.TarError as e:
            tar_error = e
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait(timeout=60) != 0:  # git said why (no such path, ...); that beats a complaint about the empty archive
            raise OpsError(422, "path_not_found" if sub else "archive_failed", (err.splitlines() or ["git archive failed"])[-1][:300])
        if tar_error:
            raise OpsError(422, "bad_archive", f"unreadable archive: {tar_error}") from tar_error
        ok = True
    finally:
        if not ok and proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    return count


def extract_tar(stream, dest: Path, *, max_bytes: int, max_files: int) -> int:
    """Write the regular files and folders of a tar stream under `dest`. Anything else is refused, and so is the whole archive."""
    total = 0
    count = 0
    root = dest.resolve()
    with tarfile.open(fileobj=stream, mode="r|") as tf:
        for m in tf:
            name = m.name
            if name.startswith("/") or "\x00" in name or "\\" in name or any(p == ".." for p in name.split("/")):
                raise OpsError(422, "unsafe_archive", f"unsafe name in the archive: {name[:80]!r}")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts:
                continue  # "./"
            if m.issym() or m.islnk():
                raise OpsError(422, "unsafe_archive", f"the archive has a link ({name[:80]!r}); extensions can't contain symlinks")
            if not (m.isreg() or m.isdir()):
                raise OpsError(422, "unsafe_archive", f"the archive has a special file ({name[:80]!r})")
            target = root.joinpath(*parts)
            if not target.is_relative_to(root):
                raise OpsError(422, "unsafe_archive", f"unsafe name in the archive: {name[:80]!r}")
            count += 1
            if count > max_files:
                raise OpsError(422, "too_big", f"more than {max_files} files")
            try:
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += m.size
                if total > max_bytes:
                    raise OpsError(422, "too_big", f"more than {max_bytes // (1024 * 1024)} MB")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                mode = 0o755 if m.mode & stat.S_IXUSR else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
                with os.fdopen(fd, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                os.chmod(target, mode)
            except OSError as e:  # a name used twice, or as both a file and a folder
                raise OpsError(422, "unsafe_archive", f"the archive's names clash at {name[:80]!r} ({e.strerror})") from e
    return count
