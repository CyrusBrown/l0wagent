"""Encrypted backups: `python -m l0w_control.backup create|restore` (used by `./l0w backup|restore`).

File layout: MAGIC, one JSON header line, then frames: a 4-byte big-endian length and the AES-256-GCM
ciphertext of up to CHUNK bytes of a tar.gz stream. Nonce = 8 random bytes (per file) + a 4-byte frame
counter; AAD = the header line + the counter + a final-frame flag, so reordering, dropping, truncating
and tampering all fail authentication. The key comes from the passphrase with scrypt.

Roots (env L0W_BACKUP_ROOT, default /vol): data/ (control's data dir), the other volumes by name, and
install/ (the install folder: only .env, extensions.toml and extensions/ are read or written).
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"L0WBAK1\n"
FORMAT = 1
CHUNK = 1 << 20
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MAX_SCRYPT_N = 2**20

# Each part is a folder under the backup root; a list restricts it to those items. Adding a part is one line.
PARTS: dict[str, list[str] | None] = {
    "data": None,
    "egress-data": None,
    "gateway-data": None,
    "agent-home": None,
    "agent-reviewer": None,
    "agent-workspace": None,
    "agent-work": None,
    "install": [".env", "extensions.toml", "extensions"],
}
DB_NAME = "control.db"
DB_SKIP = {DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"}


class BackupError(Exception):
    pass


def root() -> Path:
    return Path(os.environ.get("L0W_BACKUP_ROOT", "/vol"))


def version() -> str:
    from . import __version__

    return __version__


def derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def _aad(header: bytes, counter: int, final: bool) -> bytes:
    return header + struct.pack(">IB", counter, 1 if final else 0)


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">I", counter)


# ── writing ────────────────────────────────────────────────────────────────
class EncryptWriter:
    """A write-only file object that turns what it's given into encrypted frames."""

    def __init__(self, out, passphrase: str):
        salt = os.urandom(16)
        self.prefix = os.urandom(8)
        self.header = json.dumps(
            {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
             "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(self.prefix).decode(), "chunk": CHUNK},
            separators=(",", ":"), sort_keys=True,
        ).encode() + b"\n"
        self.aes = AESGCM(derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P))
        self.out = out
        self.buf = bytearray()
        self.counter = 0
        self.closed = False
        out.write(MAGIC + self.header)

    def _frame(self, data: bytes, final: bool) -> None:
        ct = self.aes.encrypt(_nonce(self.prefix, self.counter), data, _aad(self.header, self.counter, final))
        self.out.write(struct.pack(">I", len(ct)) + ct)
        self.counter += 1

    def write(self, data) -> int:
        self.buf += data
        while len(self.buf) > CHUNK:
            self._frame(bytes(self.buf[:CHUNK]), False)
            del self.buf[:CHUNK]
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._frame(bytes(self.buf), True)
            self.buf.clear()


# ── reading ────────────────────────────────────────────────────────────────
class DecryptReader:
    """A read-only file object over an encrypted archive; any failure raises BackupError."""

    def __init__(self, inp, passphrase: str):
        self.inp = inp
        if _read_exact(inp, len(MAGIC)) != MAGIC:
            raise BackupError("this isn't an l0w backup file (or it's damaged)")
        self.header = inp.readline(4096)
        try:
            h = json.loads(self.header)
            n, r, p = int(h["n"]), int(h["r"]), int(h["p"])
            salt = base64.b64decode(h["salt"])
            self.prefix = base64.b64decode(h["nonce"])
            if len(self.prefix) != 8:
                raise ValueError("nonce")
            if h["kdf"] != "scrypt" or not (2 <= n <= MAX_SCRYPT_N and n & (n - 1) == 0) or not (1 <= r <= 16 and 1 <= p <= 16):
                raise ValueError("kdf")
        except (ValueError, KeyError, TypeError):
            raise BackupError("the backup header is damaged") from None
        self.aes = AESGCM(derive_key(passphrase, salt, n, r, p))
        self.counter = 0
        self.buf = b""
        self.done = False
        self.pending = self._next_len()
        if self.pending is None:
            raise BackupError("the backup is truncated")

    def _next_len(self) -> int | None:
        raw = self.inp.read(4)
        if not raw:
            return None
        if len(raw) < 4:
            raise BackupError("the backup is truncated")
        n = struct.unpack(">I", raw)[0]
        if n < 16 or n > CHUNK + 16:
            raise BackupError("the backup is damaged (bad frame size)")
        return n

    def _fill(self) -> None:
        ct = _read_exact(self.inp, self.pending)
        if len(ct) < self.pending:
            raise BackupError("the backup is truncated")
        nxt = self._next_len()
        final = nxt is None
        try:
            self.buf += self.aes.decrypt(_nonce(self.prefix, self.counter), ct, _aad(self.header, self.counter, final))
        except InvalidTag:
            raise BackupError("wrong passphrase, or the backup is damaged or incomplete") from None
        self.counter += 1
        self.pending = nxt
        self.done = final

    def read(self, size: int = -1) -> bytes:
        while not self.done and (size < 0 or len(self.buf) < size):
            self._fill()
        if size < 0:
            size = len(self.buf)
        out, self.buf = self.buf[:size], self.buf[size:]
        return out


def _read_exact(f, n: int) -> bytes:
    out = b""
    while len(out) < n:
        b = f.read(n - len(out))
        if not b:
            break
        out += b
    return out


# ── create ─────────────────────────────────────────────────────────────────
def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mtime = int(datetime.now(UTC).timestamp())
    ti.mode = 0o600
    tar.addfile(ti, io.BytesIO(data))


def _present_parts(base: Path) -> list[str]:
    out = []
    for name, items in PARTS.items():
        d = base / name
        if not d.is_dir():
            continue
        if items is not None and not any((d / i).exists() or (d / i).is_symlink() for i in items):
            continue
        out.append(name)
    return out


def create(out, passphrase: str, base: Path | None = None) -> list[str]:
    base = base or root()
    parts = _present_parts(base)
    if "data" not in parts:
        raise BackupError(f"no data folder at {base / 'data'}")
    enc = EncryptWriter(out, passphrase)
    with tempfile.TemporaryDirectory(prefix="l0w-backup-") as tmp:
        snap = Path(tmp) / DB_NAME
        db = base / "data" / DB_NAME
        if db.exists():
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(snap))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        with tarfile.open(fileobj=enc, mode="w|gz") as tar:
            manifest = {"format": FORMAT, "created_at": datetime.now(UTC).isoformat(), "version": version(), "parts": parts}
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
            for name in parts:
                d = base / name
                items = PARTS[name]
                tar.add(d, arcname=name, recursive=False)
                if name == "data":
                    for child in sorted(d.iterdir()):
                        if child.name not in DB_SKIP:
                            tar.add(child, arcname=f"{name}/{child.name}")
                    if snap.exists():
                        tar.add(snap, arcname=f"{name}/{DB_NAME}")
                elif items is None:
                    for child in sorted(d.iterdir()):
                        tar.add(child, arcname=f"{name}/{child.name}")
                else:
                    for i in items:
                        if (d / i).exists() or (d / i).is_symlink():
                            tar.add(d / i, arcname=f"{name}/{i}")
    enc.close()
    return parts


# ── restore ────────────────────────────────────────────────────────────────
def _safe_name(name: str) -> list[str]:
    pp = Path(name)
    if not name or pp.is_absolute() or name.startswith("/") or "\0" in name:
        raise BackupError(f"unsafe path in the backup: {name!r}")
    segs = [s for s in name.split("/") if s not in ("", ".")]
    if not segs or ".." in segs:
        raise BackupError(f"unsafe path in the backup: {name!r}")
    return segs


def _check_member(m: tarfile.TarInfo) -> list[str]:
    segs = _safe_name(m.name)
    if segs[0] != "manifest.json" and segs[0] not in PARTS:
        raise BackupError(f"unexpected path in the backup: {m.name!r}")
    if len(segs) > 1 and PARTS.get(segs[0]) is not None and segs[1] not in PARTS[segs[0]]:
        raise BackupError(f"unexpected path in the backup: {m.name!r}")
    if m.islnk() or m.isdev() or m.isfifo():
        raise BackupError(f"links and special files aren't allowed in a backup: {m.name!r}")
    if m.issym():
        # resolve inside the part: depth below the part dir must never go negative
        depth = len(segs) - 2  # directories between the part dir and the link
        if m.linkname.startswith("/") or "\0" in m.linkname:
            raise BackupError(f"link points outside the backup: {m.name!r}")
        for s in m.linkname.split("/"):
            if s == "..":
                depth -= 1
                if depth < 0:
                    raise BackupError(f"link points outside the backup: {m.name!r}")
            elif s not in ("", "."):
                depth += 1
    elif not (m.isfile() or m.isdir()):
        raise BackupError(f"unsupported entry in the backup: {m.name!r}")
    return segs


def _chown_all(owners: list[tuple[Path, int, int]]) -> None:
    if os.geteuid() != 0:
        return
    for p, u, g in owners:
        try:
            os.lchown(p, u, g)
        except OSError:
            pass


def _wipe(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


def restore(inp, passphrase: str, base: Path | None = None, tmp_root: Path | None = None) -> dict:
    base = base or root()
    tmp_root = tmp_root or Path(os.environ.get("L0W_BACKUP_TMP") or tempfile.gettempdir())
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="l0w-restore-", dir=tmp_root) as tmp:
        stage = Path(tmp)
        reader = DecryptReader(inp, passphrase)
        manifest = None
        owners: list[tuple[Path, int, int]] = []
        try:
            with tarfile.open(fileobj=reader, mode="r|gz") as tar:
                for m in tar:
                    segs = _check_member(m)
                    if manifest is None:
                        if m.name != "manifest.json" or not m.isfile():
                            raise BackupError("the backup has no manifest")
                        try:
                            manifest = json.loads(tar.extractfile(m).read(1 << 20))
                        except ValueError:
                            raise BackupError("the manifest is damaged") from None
                        if manifest.get("format") != FORMAT:
                            raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
                        if not isinstance(manifest.get("parts"), list) or any(p not in PARTS for p in manifest["parts"]):
                            raise BackupError("the manifest lists unknown parts")
                        continue
                    if segs[0] not in manifest["parts"]:
                        raise BackupError(f"path not in the manifest's parts: {m.name!r}")
                    tar.extract(m, stage, filter=lambda mem, _p: mem.replace(mode=mem.mode & 0o7777, uid=mem.uid, gid=mem.gid))
                    owners.append((stage.joinpath(*segs), m.uid, m.gid))
            while reader.read(1 << 20):  # read to the end so the final frame is verified
                pass
        except tarfile.TarError as e:
            raise BackupError(f"the backup's contents are damaged: {e}") from None
        except (EOFError, OSError) as e:
            if isinstance(e, BackupError):
                raise
            raise BackupError(f"the backup's contents are damaged: {e}") from None
        if manifest is None:
            raise BackupError("the backup has no manifest")
        for name in manifest["parts"]:
            if not (stage / name).is_dir():
                raise BackupError(f"the backup lacks the {name} part")
        if not (stage / "data").is_dir():
            raise BackupError("the backup has no data part")
        _chown_all(owners)
        # Everything verified: now replace the targets.
        for name in manifest["parts"]:
            src, dst = stage / name, base / name
            dst.mkdir(parents=True, exist_ok=True)
            items = PARTS[name]
            if items is None:
                for child in list(dst.iterdir()):
                    _wipe(child)
                for child in src.iterdir():
                    shutil.move(str(child), dst / child.name)
            else:
                for i in items:
                    _wipe(dst / i)
                    if (src / i).exists() or (src / i).is_symlink():
                        shutil.move(str(src / i), dst / i)
        return manifest


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ("create", "restore"):
        print("usage: python -m l0w_control.backup create|restore  (passphrase in L0W_BACKUP_PASSPHRASE)", file=sys.stderr)
        return 2
    passphrase = os.environ.get("L0W_BACKUP_PASSPHRASE", "")
    if not passphrase:
        print("L0W_BACKUP_PASSPHRASE is empty", file=sys.stderr)
        return 2
    try:
        if argv[0] == "create":
            parts = create(sys.stdout.buffer, passphrase)
            sys.stdout.buffer.flush()
            print(f"backed up: {', '.join(parts)}", file=sys.stderr)
        else:
            m = restore(sys.stdin.buffer, passphrase)
            print(f"restored: {', '.join(m['parts'])} (backup of {m.get('created_at', '?')}, version {m.get('version', '?')})", file=sys.stderr)
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
