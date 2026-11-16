"""M3: the file browser, git history, the files origin and the shelf (real backend, no runner)."""

import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

PW = "correct horse battery"
AGENT = {"X-Agent-Token": "agent-secret"}


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture
def tree(tmp_path):
    ws, work = tmp_path / "workspace", tmp_path / "work"
    (ws / "knowledge").mkdir(parents=True)
    (work / "_inbox").mkdir(parents=True)
    (work / "garden" / "out").mkdir(parents=True)
    (ws / "NOW.md").write_text("# Now\n\nNothing yet.\n")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "Seed workspace")
    (ws / "NOW.md").write_text("# Now\n\nPump fix in progress.\nSee [user notes](knowledge/user.md).\n")
    (ws / "knowledge" / "user.md").write_text("Mara likes short summaries.\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "run #5 (done)")
    (work / "garden" / "out" / "summary.txt").write_text("first\n")
    (work / "garden" / "page.html").write_text("<h1>hi</h1><script>alert(1)</script>")
    (work / "garden" / "style.css").write_text("h1{color:red}")
    return tmp_path


@pytest.fixture
def c(tree, monkeypatch):
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tree / "workspace"))
    monkeypatch.setenv("L0W_WORK_DIR", str(tree / "work"))
    monkeypatch.setenv("L0W_INBOX_DIR", str(tree / "work" / "_inbox"))
    env = Env(data_dir=tree / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tree / "agent-home")
    be = SqliteBackend(env=env)
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        c.be = be  # type: ignore[attr-defined]
        yield c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def test_browse_read_and_history(c):
    root = ok(c.get("/api/files/list", params={"volume": "workspace"}))
    assert root["git"] and root["branch"] == "main"
    assert [e["name"] for e in root["entries"]] == ["knowledge", "NOW.md"]  # folders first, .git hidden
    now = next(e for e in root["entries"] if e["name"] == "NOW.md")
    assert now["last_run_id"] == 5
    assert c.get("/api/files/raw", params={"volume": "workspace", "path": "NOW.md"}).text.startswith("# Now")
    hist = ok(c.get("/api/files/history", params={"volume": "workspace", "path": "NOW.md"}))
    assert [h["message"] for h in hist] == ["run #5 (done)", "Seed workspace"]
    assert hist[0]["run_id"] == 5 and hist[0]["added"] == 2 and hist[0]["removed"] == 1
    d = ok(c.get("/api/files/diff", params={"volume": "workspace", "path": "NOW.md", "rev": hist[0]["rev"]}))
    assert "+Pump fix in progress." in d["patch"] and "-Nothing yet." in d["patch"]
    assert c.get("/api/files/list", params={"volume": "workspace", "path": "../.."}).status_code == 400
    assert c.get("/api/files/list", params={"volume": "work", "path": "nope"}).status_code == 404
    work = ok(c.get("/api/files/list", params={"volume": "work"}))
    assert not work["git"] and next(e for e in work["entries"] if e["name"] == "_inbox")["from_owner"]
    z = c.get("/api/files/zip", params={"volume": "workspace", "path": "knowledge"})
    assert z.status_code == 200 and z.content[:2] == b"PK"


def test_symlinks_are_never_followed(c, tree):
    secret = tree / "data" / "secret.key"
    os.symlink(secret, tree / "workspace" / "leak.txt")
    os.symlink(tree / "data", tree / "work" / "datadir")
    entries = {e["name"]: e for e in ok(c.get("/api/files/list", params={"volume": "workspace"}))["entries"]}
    assert entries["leak.txt"]["symlink"]
    for volume, path in (("workspace", "leak.txt"), ("work", "datadir/secret.key"), ("work", "datadir")):
        r = c.get("/api/files/raw", params={"volume": volume, "path": path})
        assert r.status_code == 400 and r.json()["error"] in ("symlink", "not_a_file"), (path, r.text)
    assert c.get("/api/files/list", params={"volume": "work", "path": "datadir"}).status_code == 400
    assert c.get("/api/files/preview-url", params={"volume": "workspace", "path": "leak.txt"}).status_code == 400
    # A planted .git symlink isn't read as a repo.
    (tree / "work" / "repo").mkdir()
    os.symlink(tree / "workspace" / ".git", tree / "work" / "repo" / ".git")
    assert ok(c.get("/api/files/list", params={"volume": "work", "path": "repo"}))["git"] is False
    # Shelving through a symlink is refused too.
    ok(c.post("/api/projects", json={"name": "Garden"}), 201)
    r = c.post("/api/projects/garden/shelf", json={"volume": "workspace", "path": "leak.txt"})
    assert r.status_code == 400


def test_html_is_served_only_from_the_files_origin(c):
    r = c.get("/api/files/raw", params={"volume": "work", "path": "garden/page.html"})
    assert r.headers["content-type"].startswith("text/plain") and r.headers["content-security-policy"] == "sandbox"
    url = ok(c.get("/api/files/preview-url", params={"volume": "work", "path": "garden/page.html"}))["url"]
    assert url.startswith("http://testserver:8001/f/")
    files = TestClient(c.be.files_app)
    path = url.split(":8001", 1)[1]
    page = files.get(path)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "set-cookie" not in page.headers
    # The folder-scoped link loads the page's relative assets, and nothing outside the folder.
    base = path.rsplit("/", 1)[0]
    assert files.get(base + "/style.css").text == "h1{color:red}"
    assert files.get(base + "/../garden/style.css").status_code in (400, 404)
    assert files.get(base.replace(base.split("/")[2], base.split("/")[2][:-2] + "xx") + "/style.css").status_code == 404  # tampered token
    assert files.get("/api/auth/me").status_code == 404
    # Ranges, for media seeking.
    media = files.get(path, headers={"Range": "bytes=0-3"})
    assert media.status_code == 206 and media.content == b"<h1>"


def test_shelf_live_snapshot_and_versions(c, tree):
    ok(c.post("/api/projects", json={"name": "Garden"}), 201)
    live = ok(c.post("/api/projects/garden/shelf", json={"volume": "work", "path": "garden/out/summary.txt", "label": "Summary", "keep_versions": 3}), 201)
    snap = ok(c.post("/api/projects/garden/shelf", json={"volume": "work", "path": "garden/out/summary.txt", "mode": "snapshot"}), 201)
    assert live["mode"] == "live" and live["versions"] == 1 and snap["versions"] == 1
    assert c.post("/api/projects/garden/shelf", json={"volume": "work", "path": "garden/out/summary.txt"}).status_code == 409
    out = tree / "work" / "garden" / "out" / "summary.txt"
    for n, text in enumerate(["second\n", "third\n", "fourth\n"], start=2):
        time.sleep(0.01)
        out.write_text(text)
        os.utime(out, (time.time() + n, time.time() + n))
        assert c.be.shelf_.check() == [live["id"]]
    assert c.be.shelf_.check() == []
    versions = ok(c.get(f"/api/shelf/{live['id']}/versions"))
    assert [v["n"] for v in versions] == [4, 3, 2]  # keeps 3
    assert c.get(f"/api/shelf/{live['id']}/raw").text == "fourth\n"
    assert c.get(f"/api/shelf/{live['id']}/raw", params={"version": 2}).text == "second\n"
    assert c.get(f"/api/shelf/{snap['id']}/raw").text == "first\n"
    out.unlink()
    assert c.be.shelf_.check() == [live["id"]]
    assert next(i for i in ok(c.get("/api/projects/garden/shelf")) if i["id"] == live["id"])["missing"]
    # The agent can shelve and remove its own items, not the owner's.
    agent = ok(c.post("/api/agent/shelf", headers=AGENT, json={"volume": "workspace", "path": "NOW.md", "project": "garden"}))
    assert agent["added_by"] == "assistant"
    assert c.delete(f"/api/agent/shelf/{live['id']}", headers=AGENT).status_code == 409
    ok(c.delete(f"/api/agent/shelf/{agent['id']}", headers=AGENT))
    url = ok(c.get(f"/api/shelf/{snap['id']}/preview-url"))["url"]
    assert TestClient(c.be.files_app).get(url.split(":8001", 1)[1]).text == "first\n"
