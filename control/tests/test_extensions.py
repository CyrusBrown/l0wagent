"""Extensions (M8): discovery, the owner's on/off, routes on both sides, the SDK context, isolation of failures,
prompt fragments, cards, managed folders, and surviving an update (a restart with a new version)."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

PW = "correct horse battery"
AGENT = {"X-Agent-Token": "agent-secret"}
EXAMPLE = Path(__file__).resolve().parents[2] / "extensions" / "examples" / "plantlog"


def make_be(tmp_path, monkeypatch) -> SqliteBackend:
    work = tmp_path / "work"
    (work / "_inbox").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(work / "_inbox"))
    monkeypatch.setenv("L0W_WORK_DIR", str(work))
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("L0W_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    return SqliteBackend(env=env)


@pytest.fixture
def exts(tmp_path):
    d = tmp_path / "extensions"
    d.mkdir()
    shutil.copytree(EXAMPLE, d / "plantlog")
    return d


@pytest.fixture
def client(tmp_path, monkeypatch, exts):
    be = make_be(tmp_path, monkeypatch)
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        c.be = be
        yield c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def agent(c, method, path, **kw):
    return c.request(method, f"/api/agent{path}", headers=AGENT, **kw)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_found_extensions_start_off_and_routes_wait_for_enable(client):
    [x] = ok(client.get("/api/extensions"))
    assert x["name"] == "plantlog" and not x["enabled"] and not x["loaded"] and x["compatible"] and x["ui"] is None
    assert client.get("/api/ext/plantlog/plants").status_code == 404
    assert ok(agent(client, "GET", "/ext")) == []
    x = ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    assert x["enabled"] and x["loaded"] and x["ui"]["module"] == "/api/extensions/plantlog/ui/plantlog.js"
    assert [p["element"] for p in x["ui"]["pages"]] == ["plantlog-page"]
    assert ok(client.get("/api/meta"))["capabilities"]["extensions"] == ["plantlog"]
    js = client.get("/api/extensions/plantlog/ui/plantlog.js")
    assert js.status_code == 200 and "text/javascript" in js.headers["content-type"] and "customElements" in js.text
    assert client.get("/api/extensions/plantlog/ui/../extension.py").status_code == 404


def test_owner_and_agent_routes_store_and_folder(client, tmp_path):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    assert ok(client.post("/api/ext/plantlog/plants", json={"plant": "Fern", "note": "new leaf"}))["name"] == "Fern"
    out = ok(agent(client, "POST", "/ext/plantlog/log", json={"plant": "Fern", "note": "drooping", "watered": True}))
    assert out["text"] == "Logged for Fern."
    [fern] = ok(client.get("/api/ext/plantlog/plants"))
    assert [n["by"] for n in fern["notes"]] == ["owner", "assistant"] and fern["last_watered"] and not fern["due"]
    md = (tmp_path / "work" / "_inbox" / "_ext" / "plantlog" / "log" / "plants.md").read_text()
    assert "drooping" in md
    listing = ok(agent(client, "GET", "/ext"))
    assert listing[0]["name"] == "plantlog" and {c["name"] for c in listing[0]["commands"]} == {"plants", "log"}
    # validation errors come back as 422, like control's own routes
    assert agent(client, "POST", "/ext/plantlog/log", json={"plant": ""}).status_code == 422


def test_owner_routes_need_the_owner_and_agent_routes_need_the_token(client, tmp_path, monkeypatch):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    anon = TestClient(client.app, client=("192.168.1.20", 5000))
    assert anon.get("/api/ext/plantlog/plants").status_code == 401
    assert client.post("/api/agent/ext/plantlog/log", json={"plant": "x"}).status_code == 401  # the owner's cookie isn't the agent token
    assert agent(client, "GET", "/ext/plantlog/plants").status_code in (404, 405)                 # owner routes aren't on the agent side


def test_settings_are_validated_and_read_by_the_extension(client):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    x = ok(client.put("/api/extensions/plantlog/settings", json={"values": {"water_every_days": 7}}))
    assert {s["key"]: s["value"] for s in x["settings"]}["water_every_days"] == 7
    assert client.put("/api/extensions/plantlog/settings", json={"values": {"water_every_days": 0}}).status_code == 400
    assert client.put("/api/extensions/plantlog/settings", json={"values": {"nope": 1}}).status_code == 400
    assert client.be.ext.setting("plantlog", "water_every_days") == 7


def test_cards_go_to_the_inbox_and_the_owner_resolves_them(client):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    ok(client.post("/api/ext/plantlog/plants", json={"plant": "Cactus", "note": "bought"}))
    ext = client.be.ext.loaded["plantlog"].ext
    job = ext.jobs[0][1]
    job()
    job()  # one card per due plant, not one per check
    [entry] = ok(client.get("/api/inbox", params={"view": "waiting", "kind": "extension"}))
    assert entry["kind"] == "extension" and entry["extension_card"]["title"] == "Water Cactus"
    assert ok(client.get("/api/inbox/counts"))["by_kind"]["extension"] == 1
    card = ok(client.post(f"/api/extension-cards/{entry['id']}/resolve", json={"action": "watered"}))
    assert card["status"] == "resolved" and card["resolution"]["action"] == "watered"
    assert wait_for(lambda: ok(client.get("/api/ext/plantlog/plants"))[0]["last_watered"] is not None)
    assert client.post(f"/api/extension-cards/{entry['id']}/resolve", json={"action": "watered"}).status_code == 409


def test_prompt_fragments_and_routine(client):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    system = client.be.runs_mgr._system()
    assert "### The Plant log extension" in system and "assistant ext plantlog log plant=NAME" in system
    assert "/work/_inbox/_ext/plantlog/log" in system
    routines = [r for r in ok(client.get("/api/routines")) if r["builtin"] == "ext:plantlog:weekly"]
    assert len(routines) == 1 and not routines[0]["enabled"]
    ok(client.patch(f"/api/routines/{routines[0]['id']}", json={"enabled": True}))
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": False}))
    assert "Plant log" not in client.be.runs_mgr._system()
    assert not next(r for r in ok(client.get("/api/routines")) if r["id"] == routines[0]["id"])["enabled"]
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))  # the owner's choice comes back
    assert next(r for r in ok(client.get("/api/routines")) if r["id"] == routines[0]["id"])["enabled"]


def test_a_broken_extension_is_disabled_with_its_error_and_control_carries_on(client, exts):
    bad = exts / "broken"
    bad.mkdir()
    (bad / "extension.toml").write_text('name = "broken"\nversion = "1"\nsdk = "1.0"\nentry = "extension.py"\n')
    (bad / "extension.py").write_text("def setup(ext):\n    raise RuntimeError('boom')\n")
    ok(client.post("/api/extensions/rescan"))
    r = client.put("/api/extensions/broken/enabled", json={"enabled": True})
    assert r.status_code == 409 and "boom" in r.json()["detail"]
    x = next(e for e in ok(client.get("/api/extensions")) if e["name"] == "broken")
    assert not x["enabled"] and "boom" in x["last_error"]
    assert client.get("/api/ext/broken/anything").status_code == 404
    assert ok(client.get("/api/meta"))["server_version"]


def test_hook_and_route_errors_are_recorded_not_raised(client, exts):
    d = exts / "hooky"
    d.mkdir()
    (d / "extension.toml").write_text('name = "hooky"\nversion = "1"\nsdk = "1.0"\nentry = "extension.py"\n')
    (d / "extension.py").write_text(
        "def setup(ext):\n"
        "    seen = []\n"
        "    ext.on('message_posted', lambda e: seen.append(e['body']) or ext.store.set('seen', seen))\n"
        "    ext.on('thread_created' if False else 'run_ended', lambda e: 1 / 0)\n"
        "    @ext.router.get('/crash')\n"
        "    def crash():\n"
        "        raise ValueError('nope')\n")
    ok(client.post("/api/extensions/rescan"))
    ok(client.put("/api/extensions/hooky/enabled", json={"enabled": True}))
    t = ok(client.post("/api/threads", json={"title": "Hello", "first_message": "hi there"}), 201)
    assert t["id"]
    assert wait_for(lambda: client.be.ext.kv_get("hooky", "data:seen") == ["hi there"])
    r = client.get("/api/ext/hooky/crash")
    assert r.status_code == 500 and r.json()["error"] == "extension_failed"
    x = next(e for e in ok(client.get("/api/extensions")) if e["name"] == "hooky")
    assert x["enabled"] and "nope" in x["last_error"]   # a failing route doesn't disable it


def test_incompatible_sdk_and_bad_manifest(client, exts):
    (exts / "future").mkdir()
    (exts / "future" / "extension.toml").write_text('name = "future"\nversion = "1"\nsdk = "2.0"\n')
    (exts / "mismatch").mkdir()
    (exts / "mismatch" / "extension.toml").write_text('name = "other"\nversion = "1"\nsdk = "1.0"\n')
    xs = {e["name"]: e for e in ok(client.post("/api/extensions/rescan"))}
    assert not xs["future"]["compatible"]
    assert "mismatch" not in xs  # never recorded: its manifest didn't parse
    r = client.put("/api/extensions/future/enabled", json={"enabled": True})
    assert r.status_code == 409 and "SDK 2.0" in r.json()["detail"]


def test_managed_folders_never_follow_symlinks(client, tmp_path):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    folder = tmp_path / "work" / "_inbox" / "_ext" / "plantlog"
    folder.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside"
    target.mkdir()
    os.symlink(target, folder / "log")
    r = client.post("/api/ext/plantlog/plants", json={"plant": "Fern"})
    assert r.status_code == 400 and "plain folder" in r.json()["detail"]
    assert list(target.iterdir()) == []
    os.unlink(folder / "log")
    (folder / "log").mkdir()
    os.symlink(target / "x.md", folder / "log" / "plants.md")
    assert client.post("/api/ext/plantlog/plants", json={"plant": "Fern"}).status_code == 400
    assert not (target / "x.md").exists()


def test_extension_survives_an_update(tmp_path, monkeypatch, exts):
    """Data, settings and the owner's on/off survive a restart with a new version of the extension."""
    be = make_be(tmp_path, monkeypatch)
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        ok(c.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
        ok(c.put("/api/extensions/plantlog/settings", json={"values": {"water_every_days": 9}}))
        ok(c.post("/api/ext/plantlog/plants", json={"plant": "Ivy", "note": "repotted"}))
    manifest = exts / "plantlog" / "extension.toml"
    manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
    be2 = make_be(tmp_path, monkeypatch)
    with TestClient(create_app(be2), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        [x] = ok(c.get("/api/extensions"))
        assert x["version"] == "0.2.0" and x["enabled"] and x["loaded"]
        assert {s["key"]: s["value"] for s in x["settings"]}["water_every_days"] == 9
        assert ok(c.get("/api/ext/plantlog/plants"))[0]["name"] == "Ivy"


def test_store_values_are_redacted(client):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    client.be.vault.set("PLANT_KEY", "sup3r-s3cret-value-123")
    ok(client.post("/api/ext/plantlog/plants", json={"plant": "Fern", "note": "key is sup3r-s3cret-value-123"}))
    raw = client.be.db.q1("SELECT value_json FROM extension_kv WHERE key='data:plants'")["value_json"]
    assert "sup3r-s3cret-value-123" not in raw and json.loads(raw)


def test_advanced_settings_and_reviewer_instructions(client):
    rows = {a["key"]: a for a in ok(client.get("/api/settings/advanced"))}
    assert rows["HOLD_MINUTES"]["value"] == 5 and rows["HOLD_MINUTES"]["unit"] == "min" and "USAGE_UNKNOWN_POLICY" not in rows
    rows = {a["key"]: a for a in ok(client.patch("/api/settings/advanced", json={"values": {"HOLD_MINUTES": 12}}))}
    assert rows["HOLD_MINUTES"]["value"] == 12 and client.be.settings.get("HOLD_MINUTES") == 12
    assert client.patch("/api/settings/advanced", json={"values": {"HOLD_MINUTES": 999}}).status_code == 400
    assert client.patch("/api/settings/advanced", json={"values": {"USAGE_UNKNOWN_POLICY": "allow"}}).status_code == 400
    ok(client.put("/api/instructions", json={"text": "Be brief.", "reviewer_text": "Push back on vague plans."}))
    ok(client.put("/api/instructions", json={"text": "Be briefer."}))  # leaving reviewer_text out keeps it
    got = ok(client.get("/api/instructions"))
    assert got["text"] == "Be briefer." and got["reviewer_text"] == "Push back on vague plans."
    assert "Push back on vague plans." in client.be.reviewer._system()
    from l0w_control import chat_settings
    [d] = chat_settings.describe(client.be, [{"setting": "advanced.HOLD_MINUTES", "value": 8}])
    assert d["before"] == "12.0" or d["before"] == "12"
    chat_settings.apply(client.be, [d])
    assert client.be.settings.get("HOLD_MINUTES") == 8
