import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
from l0wproto import docs, keys
from l0wproto import payloads as P
from starlette.testclient import TestClient

from l0w_ops.app import create_app
from l0w_ops.config import Settings

TOKEN = "ops-test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
MANIFEST = 'name = "plantlog"\ntitle = "Plant log"\nversion = "1.0.0"\nsdk = "1.0"\n'


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class Repo:
    """A local git repository the tests serve as `file://` (the production code accepts only https)."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        self.url = f"file://{path}"

    def commit(self, files: dict[str, str | bytes], msg: str = "c", *, clear: bool = True) -> str:
        for f in list(self.path.rglob("*")):
            if clear and ".git" not in f.relative_to(self.path).parts and (f.is_symlink() or f.is_file()):
                f.unlink()
        for rel, data in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data.encode() if isinstance(data, str) else data)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "remote")
    r.sha = r.commit({"extension.toml": MANIFEST, "main.py": "print('hi')\n", "web/ui.js": "//\n"})
    return r


@pytest.fixture
def settings(tmp_path):
    ext = tmp_path / "extensions"
    ext.mkdir()
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "extensions.toml").write_text("# the owner's\n")
    home = tmp_path / "home"
    home.mkdir()
    return Settings(token=TOKEN, docker_token="dock-token", extensions_dir=ext, toml_file=tmp_path / "project" / "extensions.toml",
                    home=home, allow_file_git=True, allow_private_hosts=True)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def post(client, path, body, token=TOKEN):
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def install_body(repo, **kw):
    return {"name": "plantlog", "git": repo.url, "commit": repo.sha, "source": "owner", **kw}


HUB = keys.from_seed(hashlib.sha256(b"hub-sign").digest(), "sign", "2026-11-17T21:30:00Z")
OTHER = keys.from_seed(hashlib.sha256(b"other-sign").digest(), "sign", "2026-11-17T21:30:00Z")


def service_doc(ref: dict | None, signer=HUB) -> dict:
    p = P.build_service("plants@hub.example", "custom", ["member"], extension=ref)
    return docs.sign_document("service", p, [signer])


def tar_bytes(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> io.BytesIO:
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as tf:
        for info, data in members:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    b.seek(0)
    return b
