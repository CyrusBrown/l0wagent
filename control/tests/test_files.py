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


def _link(path, copy):
    """Replace a file with a symlink to a copy of it elsewhere."""
    target = path.parent.parent.parent / (path.name + ".real")
    if path.exists():
        path.rename(target)
    else:
        target.write_text("x")
    os.symlink(target, path)


def _commondir(g):
    (g / "commondir").write_text("../other\n")


def _alternates(g):
    (g / "objects" / "info").mkdir(exist_ok=True)
    (g / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n")


@pytest.mark.parametrize("name,spoil", [
    ("head", lambda g: _link(g / "HEAD", True)),
    ("config", lambda g: _link(g / "config", True)),
    ("packed-refs", lambda g: _link(g / "packed-refs", True)),
    ("ref", lambda g: _link(g / "refs" / "heads" / "main", True)),
    ("deep-ref", lambda g: (g / "refs" / "heads" / "a").mkdir() or _link(g / "refs" / "heads" / "a" / "b", True)),
    ("commondir", _commondir),
    ("alternates", _alternates),
])
def test_a_repo_with_links_or_redirects_isnt_opened(c, tree, name, spoil):
    assert ok(c.get("/api/files/list", params={"volume": "workspace"}))["git"] is True
    spoil(tree / "workspace" / ".git")
    assert ok(c.get("/api/files/list", params={"volume": "workspace"}))["git"] is False, name
    assert ok(c.get("/api/files/history", params={"volume": "workspace", "path": "NOW.md"})) == []


def test_zip_and_raw_stream_in_chunks(c, tree, monkeypatch):
    from l0w_control import files

    monkeypatch.setattr(files, "CHUNK", 1000)
    data = os.urandom(5321) + b"tail"
    (tree / "work" / "garden" / "big.bin").write_bytes(data)
    monkeypatch.setattr(files.Storage if hasattr(files, "Storage") else type(c.be.storage), "read",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("whole file read")))
    z = c.get("/api/files/zip", params={"volume": "work", "path": "garden"})
    assert z.status_code == 200
    import io
    import zipfile

    arc = zipfile.ZipFile(io.BytesIO(z.content))
    assert arc.read("big.bin") == data and arc.read("style.css") == b"h1{color:red}"
    assert arc.testzip() is None
    r = c.get("/api/files/raw", params={"volume": "work", "path": "garden/big.bin", "download": True})
    assert r.content == data and r.headers["content-length"] == str(len(data))


def test_sandbox_header_on_every_files_response(c):
    files = TestClient(c.be.files_app, raise_server_exceptions=False)
    for path in ("/", "/nope", "/f/garbage/x", "/f/a.b/x"):
        r = files.get(path)
        assert r.status_code >= 400 and r.headers["content-security-policy"].startswith("sandbox allow-scripts"), path
        assert r.headers["x-content-type-options"] == "nosniff"


def test_files_hostname(c, monkeypatch):
    """On a hub the files app answers at its own hostname, on control's port, and nothing else of control does."""
    monkeypatch.setenv("L0W_FILES_ORIGIN", "https://files--mara.example.net")
    url = ok(c.get("/api/files/preview-url", params={"volume": "work", "path": "garden/page.html"}))["url"]
    assert url.startswith("https://files--mara.example.net/f/")
    path = url.split("example.net", 1)[1]
    for host in ("files--mara.example.net", "FILES--mara.example.net:443"):
        page = c.get(path, headers={"host": host})
        assert page.status_code == 200 and page.text.startswith("<h1>hi</h1>")
        assert page.headers["content-security-policy"].startswith("sandbox allow-scripts") and "x-frame-options" not in page.headers
    r = c.get("/api/auth/me", headers={"host": "files--mara.example.net"})
    assert r.status_code == 404 and r.headers["content-security-policy"].startswith("sandbox")
    assert c.get("/api/auth/me").status_code == 200  # the instance hostname is unchanged
    # The agent's network can't use the files hostname to reach anything.
    with TestClient(create_app(c.be), client=("10.255.255.7", 5000)) as agent:
        assert agent.get(path, headers={"host": "files--mara.example.net"}).status_code == 404


def test_raw_supports_range(c, tree):
    data = bytes(range(256)) * 4
    (tree / "work" / "garden" / "clip.mp4").write_bytes(data)
    p = {"volume": "work", "path": "garden/clip.mp4"}
    r = c.get("/api/files/raw", params=p)
    assert r.status_code == 200 and r.headers["accept-ranges"] == "bytes" and r.content == data
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-security-policy"] == "sandbox"
    for hdr, lo, hi in (("bytes=10-19", 10, 19), ("bytes=1000-", 1000, 1023), ("bytes=-24", 1000, 1023), ("bytes=1020-5000", 1020, 1023)):
        r = c.get("/api/files/raw", params=p, headers={"Range": hdr})
        assert r.status_code == 206, hdr
        assert r.content == data[lo:hi + 1] and r.headers["content-range"] == f"bytes {lo}-{hi}/1024"
        assert r.headers["content-security-policy"] == "sandbox" and r.headers["x-content-type-options"] == "nosniff"
    for hdr in ("bytes=1024-", "bytes=2000-3000", "bytes=-0"):
        r = c.get("/api/files/raw", params=p, headers={"Range": hdr})
        assert r.status_code == 416 and r.headers["content-range"] == "bytes */1024", hdr
    assert c.get("/api/files/raw", params=p, headers={"Range": "bytes=0-1,5-6"}).status_code == 200
    # HTML stays text/plain even for a range.
    r = c.get("/api/files/raw", params={"volume": "work", "path": "garden/page.html"}, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"<h1>" and r.headers["content-type"].startswith("text/plain")


def test_shelf_raw_supports_range(c, tree):
    (tree / "work" / "garden" / "song.mp3").write_bytes(b"0123456789")
    pj = ok(c.post("/api/projects", json={"name": "Garden", "idempotency_key": "g1"}), 201)
    item = ok(c.post(f"/api/projects/{pj['slug']}/shelf", json={"volume": "work", "path": "garden/song.mp3", "mode": "live"}), 201)
    r = c.get(f"/api/shelf/{item['id']}/raw", headers={"Range": "bytes=2-4"})
    assert r.status_code == 206 and r.content == b"234" and r.headers["content-range"] == "bytes 2-4/10"
    assert c.get(f"/api/shelf/{item['id']}/raw", headers={"Range": "bytes=10-"}).status_code == 416


def test_files_origin_pdf_is_sandboxed_too(c, tree):
    (tree / "work" / "garden" / "doc.pdf").write_bytes(b"%PDF-1.4 x")
    url = ok(c.get("/api/files/preview-url", params={"volume": "work", "path": "garden/doc.pdf"}))["url"]
    r = TestClient(c.be.files_app).get(url[url.index("/f/"):])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
    r = TestClient(c.be.files_app).get(url[url.index("/f/"):], headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.headers["content-security-policy"].startswith("sandbox")


def test_shelf_item_shows_the_run_that_last_updated_it(c, tree):
    ok(c.post("/api/projects", json={"name": "Garden"}), 201)
    out = tree / "work" / "garden" / "out" / "summary.txt"
    sh = c.be.shelf_
    for rid in (11, 12):
        c.be.db.ex("INSERT INTO runs(id, kind, why, status, started_at) VALUES(?,?,?,?,?)", (rid, "wakeup", "manual", "done", c.be.db.now()))
    item = ok(c.post("/api/projects/garden/shelf", json={"volume": "work", "path": "garden/out/summary.txt", "keep_versions": 5}), 201)
    c.be.db.ex("UPDATE shelf_items SET run_id=11 WHERE id=?", (item["id"],))
    c.be.db.ex("UPDATE shelf_versions SET run_id=11 WHERE item_id=?", (item["id"],))

    def shown():
        return next(i for i in ok(c.get("/api/projects/garden/shelf")) if i["id"] == item["id"])["run_id"]

    assert shown() == 11  # never updated: the creating run

    def change(text, n, run):
        out.write_text(text)
        os.utime(out, (time.time() + n, time.time() + n))
        assert sh.check(run) == [item["id"]]

    change("b\n", 2, 12)
    assert shown() == 12  # updated by run B
    change("c\n", 3, None)  # a scan with no run active
    assert shown() == 12  # does not hide the earlier update
