import io
import tarfile

import pytest
from conftest import tar_bytes

from l0w_ops.common import OpsError
from l0w_ops.gitx import extract_tar


def info(name, kind=tarfile.REGTYPE, size=0, linkname="", mode=0o644):
    t = tarfile.TarInfo(name)
    t.type, t.size, t.linkname, t.mode = kind, size, linkname, mode
    return t


def run(tmp_path, members, **kw):
    dest = tmp_path / "out"
    dest.mkdir(parents=True, exist_ok=True)
    return dest, extract_tar(tar_bytes(members), dest, max_bytes=kw.get("max_bytes", 10_000), max_files=kw.get("max_files", 100))


def refused(tmp_path, members, **kw):
    with pytest.raises(OpsError) as e:
        run(tmp_path, members, **kw)
    return e.value


def test_regular_files_and_folders(tmp_path):
    dest, n = run(tmp_path, [(info("./", tarfile.DIRTYPE), None), (info("a/", tarfile.DIRTYPE), None), (info("a/f.txt", size=2), b"hi"),
                             (info("run.sh", size=1, mode=0o755), b"x"), (info("g.txt", size=1, mode=0o600), b"y")])
    assert (dest / "a" / "f.txt").read_bytes() == b"hi" and n == 4
    assert (dest / "run.sh").stat().st_mode & 0o111 and not (dest / "g.txt").stat().st_mode & 0o111
    assert (dest / "g.txt").stat().st_mode & 0o044  # readable by control


@pytest.mark.parametrize("name", ["../evil", "a/../../evil", "/etc/evil", "a\\b", "a/../b"])
def test_unsafe_names(tmp_path, name):
    e = refused(tmp_path, [(info(name, size=1), b"x")])
    assert e.code == "unsafe_archive"
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_are_refused(tmp_path, kind):
    e = refused(tmp_path, [(info("ok", size=1), b"x"), (info("l", kind, linkname="/etc/passwd"), None)])
    assert e.code == "unsafe_archive" and "link" in e.detail


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_devices_are_refused(tmp_path, kind):
    assert refused(tmp_path, [(info("dev", kind), None)]).code == "unsafe_archive"


def test_clashing_names_are_refused(tmp_path):
    assert refused(tmp_path / "1", [(info("a", size=1), b"x"), (info("a", size=1), b"y")]).code == "unsafe_archive"
    assert refused(tmp_path / "2", [(info("a", size=1), b"x"), (info("a/b", size=1), b"y")]).code == "unsafe_archive"


def test_a_file_cant_be_written_through_a_link_that_came_earlier(tmp_path):
    # the link is refused outright, so nothing after it is written either
    outside = tmp_path / "outside"
    outside.mkdir()
    e = refused(tmp_path, [(info("l", tarfile.SYMTYPE, linkname=str(outside)), None), (info("l/pwn", size=1), b"x")])
    assert e.code == "unsafe_archive" and not (outside / "pwn").exists()


def test_size_and_count_limits(tmp_path):
    assert refused(tmp_path / "1", [(info("a", size=600), b"x" * 600), (info("b", size=600), b"x" * 600)], max_bytes=1000).code == "too_big"
    assert refused(tmp_path / "2", [(info(f"f{i}", size=0), b"") for i in range(5)], max_files=3).code == "too_big"


def test_not_a_tar(tmp_path):
    dest = tmp_path / "o"
    dest.mkdir()
    with pytest.raises(tarfile.TarError):
        extract_tar(io.BytesIO(b"definitely not a tar file" * 100), dest, max_bytes=100, max_files=10)
