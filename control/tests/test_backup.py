"""Encrypted backups: round trip, wrong passphrase, tampering, unsafe paths."""

import io
import json
import sqlite3
import struct
import tarfile
from pathlib import Path

import pytest

from l0w_control import backup

PW = "correct horse"


def make_src(base: Path) -> None:
    (base / "data").mkdir(parents=True)
    db = sqlite3.connect(base / "data" / "control.db")
    db.execute("create table t (v text)")
    db.execute("insert into t values ('hello')")
    db.commit()
    db.close()
    (base / "data" / "secret.key").write_text("vault-key")
    for v in ("egress-data", "gateway-data", "agent-home", "agent-reviewer", "agent-workspace", "agent-work"):
        (base / v / "sub").mkdir(parents=True)
        (base / v / "sub" / "f.txt").write_text(v)
    (base / "install" / "extensions" / "plantlog").mkdir(parents=True)
    (base / "install" / "extensions" / "plantlog" / "ext.py").write_text("x = 1")
    (base / "install" / ".env").write_text("L0W_AGENT_TOKEN=abc\n")
    (base / "install" / "compose.yaml").write_text("ignored")


def archive(tmp_path: Path, pw: str = PW) -> bytes:
    src = tmp_path / "src"
    make_src(src)
    out = io.BytesIO()
    backup.create(out, pw, src)
    return out.getvalue()


def test_round_trip(tmp_path):
    blob = archive(tmp_path)
    dst = tmp_path / "dst"
    (dst / "data").mkdir(parents=True)
    (dst / "data" / "stale").write_text("old")
    m = backup.restore(io.BytesIO(blob), PW, dst, tmp_path / "tmp")
    assert m["format"] == 1 and "data" in m["parts"] and "install" in m["parts"]
    assert not (dst / "data" / "stale").exists()
    assert sqlite3.connect(dst / "data" / "control.db").execute("select v from t").fetchall() == [("hello",)]
    assert (dst / "data" / "secret.key").read_text() == "vault-key"
    for v in ("egress-data", "gateway-data", "agent-home", "agent-reviewer", "agent-workspace", "agent-work"):
        assert (dst / v / "sub" / "f.txt").read_text() == v
    assert (dst / "install" / ".env").read_text() == "L0W_AGENT_TOKEN=abc\n"
    assert (dst / "install" / "extensions" / "plantlog" / "ext.py").read_text() == "x = 1"
    assert not (dst / "install" / "compose.yaml").exists()
    assert not (dst / "agent-cache").exists()


def snapshot(p: Path):
    return sorted((str(f.relative_to(p)), f.read_bytes() if f.is_file() else None) for f in p.rglob("*"))


def target(tmp_path: Path) -> Path:
    dst = tmp_path / "dst"
    (dst / "data").mkdir(parents=True, exist_ok=True)
    (dst / "data" / "keep").write_text("mine")
    (dst / "install").mkdir(exist_ok=True)
    (dst / "install" / ".env").write_text("old")
    return dst


def test_wrong_passphrase_touches_nothing(tmp_path):
    blob = archive(tmp_path)
    dst = target(tmp_path)
    before = snapshot(dst)
    with pytest.raises(backup.BackupError, match="wrong passphrase"):
        backup.restore(io.BytesIO(blob), "nope", dst, tmp_path / "tmp")
    assert snapshot(dst) == before


def frames(blob: bytes):
    head_end = len(backup.MAGIC) + blob[len(backup.MAGIC):].index(b"\n") + 1
    out, i = [], head_end
    while i < len(blob):
        n = struct.unpack(">I", blob[i:i + 4])[0]
        out.append(blob[i:i + 4 + n])
        i += 4 + n
    return blob[:head_end], out


def bigger(tmp_path):
    src = tmp_path / "src"
    make_src(src)
    import os
    (src / "agent-work" / "big.bin").write_bytes(os.urandom(3 * backup.CHUNK))
    out = io.BytesIO()
    backup.create(out, PW, src)
    return out.getvalue()


def check_fails(tmp_path, blob):
    dst = target(tmp_path)
    before = snapshot(dst)
    with pytest.raises(backup.BackupError):
        backup.restore(io.BytesIO(blob), PW, dst, tmp_path / "tmp")
    assert snapshot(dst) == before


def test_flipped_byte(tmp_path):
    blob = bytearray(bigger(tmp_path))
    blob[len(blob) // 2] ^= 1
    check_fails(tmp_path, bytes(blob))


def test_dropped_frame(tmp_path):
    head, fr = frames(bigger(tmp_path))
    assert len(fr) > 3
    check_fails(tmp_path, head + b"".join(fr[:1] + fr[2:]))


def test_reordered_frames(tmp_path):
    head, fr = frames(bigger(tmp_path))
    check_fails(tmp_path, head + b"".join([fr[1], fr[0], *fr[2:]]))


def test_truncated(tmp_path):
    blob = bigger(tmp_path)
    head, fr = frames(blob)
    check_fails(tmp_path, head + b"".join(fr[:-1]))  # whole last frame gone
    check_fails(tmp_path, blob[:-5])  # cut mid-frame
    check_fails(tmp_path, blob[: len(backup.MAGIC)])


def evil(tmp_path, build) -> bytes:
    out = io.BytesIO()
    enc = backup.EncryptWriter(out, PW)
    with tarfile.open(fileobj=enc, mode="w|gz") as tar:
        ti = tarfile.TarInfo("manifest.json")
        data = json.dumps({"format": 1, "parts": ["data"]}).encode()
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
        build(tar)
    enc.close()
    return out.getvalue()


def test_dotdot_refused(tmp_path):
    def build(tar):
        ti = tarfile.TarInfo("data/../x")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))

    check_fails(tmp_path, evil(tmp_path, build))
    assert not (tmp_path / "x").exists()


def test_absolute_and_escaping_links_refused(tmp_path):
    def absolute(tar):
        ti = tarfile.TarInfo("/etc/x")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))

    def link(tar):
        ti = tarfile.TarInfo("data/l")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "../../etc"
        tar.addfile(ti)

    def hard(tar):
        ti = tarfile.TarInfo("data/h")
        ti.type = tarfile.LNKTYPE
        ti.linkname = "data/x"
        tar.addfile(ti)

    for b in (absolute, link, hard):
        check_fails(tmp_path, evil(tmp_path, b))


def test_inner_symlink_allowed(tmp_path):
    def build(tar):
        ti = tarfile.TarInfo("data/l")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "a/b"
        tar.addfile(ti)

    dst = tmp_path / "dst"
    backup.restore(io.BytesIO(evil(tmp_path, build)), PW, dst, tmp_path / "tmp")
    assert (dst / "data" / "l").is_symlink()
