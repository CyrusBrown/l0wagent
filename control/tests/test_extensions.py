"""Extensions (M8): discovery, the owner's on/off, routes on both sides, the SDK context, isolation of failures,
prompt fragments, cards, managed folders, and surviving an update (a restart with a new version)."""

import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from l0wproto import b64, docs, keys, spaces

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

from .hubworld import BOARD, ECHO, ME, wait_sent

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
    assert "/external/plantlog/log" in system
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


# ── ext.hub (SDK 1.1): a hub's services ─────────────────────────────────────
HUBBY = '''
from l0w_control.sdk import HubCallError, HubUnavailable


def setup(ext):
    def err(e):
        return {"ok": False, "code": e.code, "detail": e.detail, "from_service": e.from_service, "unavailable": isinstance(e, HubUnavailable)}

    @ext.router.get("/avail")
    def avail():
        return ext.hub.available()

    @ext.router.post("/call")
    async def call(body: dict):
        try:
            return {"ok": True, "result": await ext.hub.acall(body["service"], body["method"], body.get("args"), body.get("role"), timeout=body.get("timeout", 5))}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/call-sync")
    def call_sync(body: dict):
        try:
            return {"ok": True, "result": ext.hub.call(body["service"], body["method"], body.get("args"), body.get("role"), timeout=body.get("timeout", 5))}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/call-in-loop")
    async def call_in_loop(body: dict):
        try:
            return {"ok": True, "result": ext.hub.call("echo", "echo", {})}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "type": type(e).__name__, "detail": str(e)}

    ext.hub.on_event("custom", lambda e: ext.store.set("by_kind", e))
    ext.hub.on_event("echo@hub.example", lambda e: ext.store.set("by_address", e))
    ext.hub.on_event("board", lambda e: ext.store.set("board", e))

    async def later(e):
        ext.store.set("async_handler", e["event"])
    ext.hub.on_event("echo@hub.example", later)
'''


@pytest.fixture
def hubby(hw):
    d = Path(hw.be.ext.root) / "hubby"
    d.mkdir()
    (d / "extension.toml").write_text('name = "hubby"\nversion = "1"\nsdk = "1.0"\nentry = "extension.py"\n')
    (d / "extension.py").write_text(HUBBY)
    ok(hw.c.post("/api/extensions/rescan"))
    ok(hw.c.put("/api/extensions/hubby/enabled", json={"enabled": True}))
    return hw


def in_thread(fn, *a, **kw):
    from concurrent.futures import ThreadPoolExecutor

    return ThreadPoolExecutor(max_workers=1).submit(fn, *a, **kw)


def test_hub_sdk_is_empty_without_a_connected_service_and_refuses_calls(hubby):
    hw = hubby
    assert ok(hw.c.get("/api/ext/hubby/avail")) == []  # nothing synced yet
    r = ok(hw.c.post("/api/ext/hubby/call", json={"service": "echo", "method": "echo"}))
    assert r["ok"] is False and r["code"] == "unknown_service" and r["unavailable"] is True
    hw.sync()  # connecting is not connected
    assert ok(hw.c.get("/api/ext/hubby/avail")) == []
    r = ok(hw.c.post("/api/ext/hubby/call", json={"service": "echo", "method": "echo"}))
    assert r["code"] == "not_connected" and r["unavailable"] is True


def test_hub_sdk_available_calls_roles_and_errors(hubby):
    hw = hubby
    hw.connect_echo()
    assert ok(hw.c.get("/api/ext/hubby/avail")) == [{"address": ECHO, "kind": "custom", "title": "Echo", "roles": ["tester"]}]
    # an async route awaits the call; the reply comes in while it waits
    f = in_thread(hw.c.post, "/api/ext/hubby/call", json={"service": "echo", "method": "secret", "args": {"n": 1}, "role": "tester"})
    [out] = wait_sent(hw)
    assert out["body"]["args"] == {"n": 1} and out["body"]["role"]["type"] == "role-grant"
    hw.reply(out["body"]["call"], ok=True, result={"secret": "xyzzy"})
    assert f.result(10).json() == {"ok": True, "result": {"secret": "xyzzy"}}
    row = hw.be.db.q1("SELECT * FROM service_calls ORDER BY created_at DESC, rowid DESC")
    assert row["by"] == "extension:hubby" and row["role"] == "tester"
    # a plain `def` route (a worker thread) uses call_sync; a service's own error code comes through
    f = in_thread(hw.c.post, "/api/ext/hubby/call-sync", json={"service": ECHO, "method": "secret"})
    out = wait_sent(hw, 2)[-1]
    assert out["body"]["role"] is None
    hw.reply(out["body"]["call"], ok=False, error={"code": "not_allowed", "detail": "needs the tester role"})
    assert f.result(10).json() == {"ok": False, "code": "not_allowed", "detail": "needs the tester role", "from_service": True, "unavailable": False}
    # a role it doesn't hold, and a call nobody answers
    r = ok(hw.c.post("/api/ext/hubby/call", json={"service": "echo", "method": "echo", "role": "admin"}))
    assert r["code"] == "no_role" and r["from_service"] is False
    r = ok(hw.c.post("/api/ext/hubby/call", json={"service": "echo", "method": "echo", "timeout": 0.3}))
    assert r["code"] == "timeout"
    # call_sync from a coroutine would block control's loop: refused
    r = ok(hw.c.post("/api/ext/hubby/call-in-loop", json={}))
    assert r["ok"] is False and r["type"] == "ExtensionError" and "await" in r["detail"]


def test_hub_sdk_events_go_to_the_extensions_that_asked(hubby):
    hw = hubby
    hw.connect_echo()
    assert hw.arrive(ECHO, "service-event", {"event": "post.new", "data": {"title": "hi"}}).status_code == 202
    expected = {"service": ECHO, "kind": "custom", "event": "post.new", "data": {"title": "hi"}}
    assert wait_for(lambda: hw.be.ext.kv_get("hubby", "data:by_kind") == expected and hw.be.ext.kv_get("hubby", "data:by_address") == expected)
    assert wait_for(lambda: hw.be.ext.kv_get("hubby", "data:async_handler") == "post.new")  # an async handler runs on control's loop
    assert hw.be.ext.kv_get("hubby", "data:board") is None  # asked for another kind
    # a roles change is control's own business, not an event for extensions
    assert hw.arrive(ECHO, "service-event", {"event": "roles", "data": {"grants": []}}).status_code == 202
    time.sleep(0.3)
    assert hw.be.ext.kv_get("hubby", "data:by_kind") == expected


def test_git_sources_come_from_extensions_toml_and_the_ops_file(client, exts):
    """extensions.toml (the owner's) and extensions/.ops.toml (written by the ops container) both name sources; the owner's wins."""
    mgr = client.be.ext
    sha = "a" * 40
    (exts.parent / "extensions.toml").write_text(f'[extension.plantlog]\ngit = "https://x/mine.git"\ncommit = "{sha}"\n')
    (exts / ".ops.toml").write_text(f'[extension.plantlog]\ngit = "https://x/ui.git"\ncommit = "{"b" * 40}"\n[extension.other]\ngit = "https://x/o.git"\ncommit = "{"c" * 40}"\n')
    assert mgr._sources() == {"plantlog": f"https://x/mine.git@{sha[:12]}", "other": f"https://x/o.git@{'c' * 12}"}
    (exts.parent / "extensions.toml").unlink()
    assert mgr._sources()["plantlog"].startswith("https://x/ui.git@")
    (exts / ".ops.toml").write_text("not [toml")
    assert mgr._sources() == {}


# ── SDK 1.2: what a shared-spaces extension needs ───────────────────────────
SPACEY = '''
import base64
from l0w_control.sdk import HubCallError, HubUnavailable


def setup(ext):
    def err(e):
        return {"ok": False, "code": e.code, "detail": e.detail, "from_service": e.from_service, "data": e.data, "unavailable": isinstance(e, HubUnavailable)}

    @ext.router.post("/sign")
    def sign(body: dict):
        try:
            return {"ok": True, "doc": ext.hub.sign_space_version(body["payload"])}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/asign")
    async def asign(body: dict):
        try:
            return {"ok": True, "doc": await ext.hub.asign_space_version(body["payload"])}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/open")
    def open_(body: dict):
        try:
            return {"ok": True, "key": base64.b64encode(ext.hub.open_space_key(body["doc"], body["creator"])).decode()}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/aopen")
    async def aopen(body: dict):
        try:
            return {"ok": True, "key": base64.b64encode(await ext.hub.aopen_space_key(body["doc"], body["creator"])).decode()}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/put")
    def put(body: dict):
        try:
            ext.hub.transfer_put(body["service"], body["url"], base64.b64decode(body["data"]))
            return {"ok": True}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/get")
    def get(body: dict):
        try:
            return {"ok": True, "data": base64.b64encode(ext.hub.transfer_get(body["service"], body["url"], body["max"])).decode()}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/aput")
    async def aput(body: dict):
        try:
            await ext.hub.atransfer_put(body["service"], body["url"], base64.b64decode(body["data"]))
            return {"ok": True}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/aget")
    async def aget(body: dict):
        try:
            return {"ok": True, "data": base64.b64encode(await ext.hub.atransfer_get(body["service"], body["url"], body["max"])).decode()}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/call")
    async def call(body: dict):
        try:
            return {"ok": True, "result": await ext.hub.acall(body["service"], body["method"], body.get("args"), timeout=5)}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/sign-key")
    def sign_key(body: dict):
        try:
            return {"ok": True, "doc": ext.hub.sign_space_key(body["payload"])}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/member-keys")
    async def member_keys(body: dict):
        try:
            return {"ok": True, "keys": await ext.hub.amember_keys(body["address"])}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/member-keys-sync")
    def member_keys_sync(body: dict):
        try:
            return {"ok": True, "keys": ext.hub.member_keys(body["address"])}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/my-keys")
    def my_keys(body: dict):
        try:
            return {"ok": True, "keys": ext.hub.my_keys()}
        except HubCallError as e:
            return err(e)

    @ext.router.post("/has-secret")
    def has_secret(body: dict):
        return {"text": ext.vault.contains_secret(body["text"]), "bytes": ext.vault.contains_secret(body["text"].encode())}

    @ext.router.post("/in-loop")
    async def in_loop(body: dict):
        try:
            return {"ok": True, "doc": ext.hub.sign_space_version({})}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "type": type(e).__name__, "detail": str(e)}
'''
CREATOR = "bob@hub.example"
SPACE = "sp-1"
SPACE_KEY = bytes(range(32))


@pytest.fixture
def spacey(hw):
    d = Path(hw.be.ext.root) / "spacey"
    d.mkdir()
    (d / "extension.toml").write_text('name = "spacey"\nversion = "1"\nsdk = "1.2"\nentry = "extension.py"\n')
    (d / "extension.py").write_text(SPACEY)
    ok(hw.c.post("/api/extensions/rescan"))
    ok(hw.c.put("/api/extensions/spacey/enabled", json={"enabled": True}))
    hw.creator = keys.generate("sign")
    hw.gw.sign_keys[CREATOR] = hw.creator
    return hw


def test_the_sdk_declares_1_2():
    from l0w_control import sdk

    assert sdk.SDK_VERSION == "1.2"


def version_payload(author=ME, **over):
    meta = spaces.meta_seal(SPACE_KEY, SPACE, 1, author, {"files": {}})
    return {**spaces.build_space_version(SPACE, 1, None, 1, author, [], meta), **over}


@pytest.mark.parametrize("route", ["sign", "asign"])
def test_hub_sdk_signs_a_space_version_through_the_gateway(spacey, route):
    hw = spacey
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"payload": version_payload()}))
    assert r["ok"] is True and isinstance(r["doc"], str)
    doc = json.loads(r["doc"])
    assert r["doc"] == spaces.version_doc_bytes(doc).decode()
    p = spaces.verify_space_version(doc, hw.gw.me_sign.key, space=SPACE, author=ME)
    assert p["seq"] == 1 and spaces.valid_version_id(spaces.version_id_of(doc))
    assert hw.gw.bodies("/v0/sign/space-version")[-1]["payload"]["author"] == ME
    # the gateway's refusals come back as HubCallError, with the gateway's reason
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"payload": version_payload(author="eve@hub.example")}))
    assert r["ok"] is False and r["code"] == "refused" and "own versions" in r["detail"] and r["from_service"] is False
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"payload": version_payload(seq=0)}))
    assert r["code"] == "bad_args"
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"payload": "nope"}))
    assert r["code"] == "bad_args"


def test_signing_needs_a_hub_and_the_sync_form_refuses_the_loop(spacey):
    hw = spacey
    r = ok(hw.c.post("/api/ext/spacey/in-loop", json={}))
    assert r["ok"] is False and r["type"] == "ExtensionError" and "await" in r["detail"]
    hw.be.db.ex("UPDATE hub_membership SET state='left' WHERE id=1")
    r = ok(hw.c.post("/api/ext/spacey/sign", json={"payload": version_payload()}))
    assert r["ok"] is False and r["code"] == "not_joined" and r["unavailable"] is True


def space_key_doc(hw, *, granter=CREATOR, member=ME, member_kid=None, signer=None, sealed_to=None, key=SPACE_KEY, epoch=1):
    seal_kp = hw.gw.me_seal
    member_kid = member_kid or seal_kp.kid
    creator = signer or hw.creator
    sealed = spaces.seal_space_key(key, (sealed_to or seal_kp).key, space=SPACE, epoch=epoch, member=member, member_kid=member_kid, granter=granter,
                                   granter_kid=creator.kid)
    p = spaces.build_space_key(SPACE, epoch, member, member_kid, granter, creator.kid, sealed)
    return docs.sign_document("space-key", p, [creator])


@pytest.mark.parametrize("route", ["open", "aopen"])
def test_hub_sdk_opens_a_space_key_after_checking_the_granters_signature(spacey, route):
    import base64

    hw = spacey
    doc = space_key_doc(hw)
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"doc": doc, "creator": CREATOR}))
    assert r["ok"] is True and base64.b64decode(r["key"]) == SPACE_KEY
    assert hw.gw.bodies("/v0/peer/pin")[-1] == {"address": CREATOR}  # the creator's key came from the directory (pinned at the gateway)
    assert hw.gw.bodies("/v0/space/open-key")[-1] == {"doc": doc}
    # a JSON string works too, and the creator's address is case-insensitive
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"doc": json.dumps(doc), "creator": CREATOR.upper()}))
    assert base64.b64decode(r["key"]) == SPACE_KEY


def test_a_space_key_that_does_not_check_out_never_reaches_the_gateway(spacey):
    hw = spacey
    stranger = keys.generate("sign")

    def attempt(doc, creator=CREATOR):
        n = len(hw.gw.bodies("/v0/space/open-key"))
        r = ok(hw.c.post("/api/ext/spacey/open", json={"doc": doc, "creator": creator}))
        assert len(hw.gw.bodies("/v0/space/open-key")) == n  # control said no first
        return r

    # signed by a key that isn't the creator's directory key (granter_kid names the stranger's key)
    r = attempt(space_key_doc(hw, signer=stranger))
    assert r["ok"] is False and r["code"] == "bad_signature"
    # the granter isn't the space's creator
    r = attempt(space_key_doc(hw, granter="eve@hub.example", signer=stranger))
    assert r["code"] == "bad_signature" and "creator" in r["detail"]
    # a key for another member
    r = attempt(space_key_doc(hw, member="eve@hub.example"))
    assert r["code"] == "not_for_us"
    # a payload tampered with after signing
    doc = space_key_doc(hw)
    p = spaces.space_key_payload(doc)
    forged = {**doc, "payload": b64.encode(json.dumps({**p, "epoch": 5}, separators=(",", ":")).encode())}
    assert attempt(forged)["code"] == "bad_signature"
    # not a space-key document at all, nor JSON
    assert attempt({"type": "envelope"})["code"] == "bad_args"
    assert attempt("not json")["code"] == "bad_args"
    assert attempt(doc, creator="")["code"] == "bad_args"


def test_a_space_key_sealed_to_another_key_fails_at_the_gateway_and_directory_trouble_is_named(spacey):
    hw = spacey
    other = keys.generate("seal")
    r = ok(hw.c.post("/api/ext/spacey/open", json={"doc": space_key_doc(hw, sealed_to=other), "creator": CREATOR}))
    assert r["ok"] is False and r["code"] == "bad_key"
    hw.gw.errors["/v0/peer/pin"] = (409, "key_changed", "")
    r = ok(hw.c.post("/api/ext/spacey/open", json={"doc": space_key_doc(hw), "creator": CREATOR}))
    assert r["code"] == "key_changed"
    hw.gw.errors["/v0/peer/pin"] = (404, "no_such_member", "")
    r = ok(hw.c.post("/api/ext/spacey/open", json={"doc": space_key_doc(hw), "creator": CREATOR}))
    assert r["code"] == "unknown_member"


# transfers: the service's own hostname only
HOST = "echo.hub.example"


@pytest.fixture
def transfers(spacey):
    hw = spacey
    hw.gw.hostnames[ECHO] = {"instance": HOST, "files": "files--echo.hub.example"}
    hw.connect_echo()
    seen: list = []
    store: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.startswith("/redirect"):
            return httpx.Response(302, headers={"Location": f"https://{HOST}/v0/blob/ok"})
        if path.startswith("/slow-big"):
            return httpx.Response(200, content=b"x" * 5000)
        if path.startswith("/denied"):
            return httpx.Response(403)
        if request.method == "PUT":
            store[path] = request.content
            return httpx.Response(204)
        return httpx.Response(200, content=store[path]) if path in store else httpx.Response(404)

    hw.be.membership_.transport = httpx.MockTransport(handler)
    hw.seen, hw.store = seen, store
    return hw


def b(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


@pytest.mark.parametrize(("put", "get"), [("put", "get"), ("aput", "aget")])
def test_hub_sdk_transfers_to_and_from_the_services_own_hostname(transfers, put, get):
    hw = transfers
    url = f"https://{HOST}/v0/blob/e1.abc"
    assert ok(hw.c.post(f"/api/ext/spacey/{put}", json={"service": "echo", "url": url, "data": b(b"\x01ciphertext")})) == {"ok": True}
    assert hw.store["/v0/blob/e1.abc"] == b"\x01ciphertext"
    r = ok(hw.c.post(f"/api/ext/spacey/{get}", json={"service": ECHO, "url": url, "max": 100}))
    assert r["ok"] is True and r["data"] == b(b"\x01ciphertext")
    # never instance credentials: no Authorization, no cookie, no control token or agent token anywhere
    for req in hw.seen:
        assert not {h.lower() for h in req.headers} & {"authorization", "cookie", "x-agent-token", "x-l0w-client", "proxy-authorization"}
        assert str(req.url).startswith(f"https://{HOST}/")


def test_transfers_refuse_other_hosts_schemes_and_userinfo(transfers):
    hw = transfers
    for url in ("https://evil.example/v0/blob/x", f"https://{HOST}.evil.example/x", f"https://evil.example@{HOST}/x", f"https://{HOST}@evil.example/x",
                f"http://{HOST}/x", "https://x/y", "ftp://echo.hub.example/x", "echo.hub.example/x", "", f"https://{HOST}:8443/x", "https://127.0.0.1/x"):
        for route, extra in (("put", {"data": b(b"x")}), ("get", {"max": 10})):
            r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"service": "echo", "url": url, **extra}))
            assert r["ok"] is False and r["code"] == "bad_url", (route, url, r)
    assert hw.seen == []  # nothing went out


def test_transfers_need_a_connected_service_and_follow_no_redirects(transfers):
    hw = transfers
    # a service that exists but isn't switched on (board is off), and one that doesn't exist
    hw.gw.hostnames[BOARD] = {"instance": "board.hub.example", "files": "f"}
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "board", "url": "https://board.hub.example/x", "max": 10}))
    assert r["code"] == "not_connected" and r["unavailable"] is True
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "nobody", "url": f"https://{HOST}/x", "max": 10}))
    assert r["code"] == "unknown_service"
    # a redirect (even to the same host) is an error, not followed
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "echo", "url": f"https://{HOST}/redirect", "max": 10}))
    assert r["code"] == "transfer_failed" and "302" in r["detail"]
    assert [str(q.url) for q in hw.seen] == [f"https://{HOST}/redirect"]
    r = ok(hw.c.post("/api/ext/spacey/put", json={"service": "echo", "url": f"https://{HOST}/denied", "data": b(b"x")}))
    assert r["code"] == "transfer_failed" and "403" in r["detail"]


def test_transfer_get_is_size_limited(transfers):
    hw = transfers
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "echo", "url": f"https://{HOST}/slow-big", "max": 4999}))
    assert r["ok"] is False and r["code"] == "too_large"
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "echo", "url": f"https://{HOST}/slow-big", "max": 5000}))
    assert r["ok"] is True and len(r["data"]) > 0
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "echo", "url": f"https://{HOST}/slow-big", "max": -1}))
    assert r["code"] == "bad_args"


def test_transfer_put_is_size_limited(transfers, monkeypatch):
    from l0w_control import hubservices

    hw = transfers
    monkeypatch.setattr(hubservices, "TRANSFER_PUT_MAX", 10)
    r = ok(hw.c.post("/api/ext/spacey/put", json={"service": "echo", "url": f"https://{HOST}/x", "data": b(b"x" * 11)}))
    assert r["code"] == "too_large" and hw.seen == []


def test_outside_development_transfers_go_through_the_vetted_transport(transfers):
    from l0w_control.safefetch import PinnedAsyncTransport

    hw = transfers
    m = hw.be.membership_
    m.transport, m.allow_http, m.connect_to = None, False, None
    assert isinstance(hw.hs._transfer_client()._transport, PinnedAsyncTransport)
    m.allow_http = True  # development: no vetting, so a fake hub on a private address works
    assert not isinstance(hw.hs._transfer_client()._transport, PinnedAsyncTransport)


def test_transfers_are_refused_while_peer_activity_is_paused(transfers):
    hw = transfers
    hw.be.settings.set("PEER_PAUSED", True)
    r = ok(hw.c.post("/api/ext/spacey/get", json={"service": "echo", "url": f"https://{HOST}/x", "max": 10}))
    assert r["code"] == "paused"


def test_transfers_honour_the_development_settings(transfers):
    """L0W_HUB_ALLOW_HTTP lets an http URL on the service's hostname through; L0W_HUB_CONNECT_TO sends it to the fake hub with the real Host."""
    hw = transfers
    m = hw.be.membership_
    m.allow_http = True
    m.connect_to = "127.0.0.1:8770"
    hw.gw.hostnames[ECHO] = {"instance": "echo.hub.localhost:8770", "files": "f"}
    hw.hs._hostnames.clear()
    r = ok(hw.c.post("/api/ext/spacey/put", json={"service": "echo", "url": "http://echo.hub.localhost:8770/v0/blob/e1.z", "data": b(b"dev")}))
    assert r["ok"] is True, r
    req = hw.seen[-1]
    assert str(req.url) == "http://127.0.0.1:8770/v0/blob/e1.z" and req.headers["host"] == "echo.hub.localhost:8770"
    # still only that hostname
    r = ok(hw.c.post("/api/ext/spacey/put", json={"service": "echo", "url": "http://evil.example:8770/x", "data": b(b"dev")}))
    assert r["code"] == "bad_url"


def test_hubcallerror_carries_the_extra_fields_of_a_service_error(spacey):
    hw = spacey
    hw.connect_echo()
    f = in_thread(hw.c.post, "/api/ext/spacey/call", json={"service": "echo", "method": "version.push", "args": {"space": SPACE}})
    [out] = wait_sent(hw)
    head = {"seq": 3, "version": "v." + "A" * 22}
    hw.reply(out["body"]["call"], ok=False, error={"code": "conflict", "detail": "the head moved", "head": head})
    r = f.result(10).json()
    assert r["ok"] is False and r["code"] == "conflict" and r["from_service"] is True and r["data"] == {"head": head}
    row = hw.be.db.q1("SELECT * FROM service_calls ORDER BY created_at DESC, rowid DESC")
    assert json.loads(row["result_json"])["data"] == {"head": head}
    # an error with nothing extra has an empty `data`; secrets in it are redacted; an oversized one is dropped
    hw.be.vault.set("SPACE_KEY", "sup3r-s3cret-value-123")
    f = in_thread(hw.c.post, "/api/ext/spacey/call", json={"service": "echo", "method": "lock.take"})
    out = wait_sent(hw, 2)[-1]
    hw.reply(out["body"]["call"], ok=False, error={"code": "locked", "detail": "x", "lock": {"holder": "bob@hub.example", "note": "sup3r-s3cret-value-123"}})
    r = f.result(10).json()
    assert r["code"] == "locked" and r["data"]["lock"]["holder"] == "bob@hub.example" and "sup3r-s3cret-value-123" not in json.dumps(r)
    f = in_thread(hw.c.post, "/api/ext/spacey/call", json={"service": "echo", "method": "x"})
    out = wait_sent(hw, 3)[-1]
    hw.reply(out["body"]["call"], ok=False, error={"code": "not_allowed", "detail": "no"})
    assert f.result(10).json()["data"] == {}
    f = in_thread(hw.c.post, "/api/ext/spacey/call", json={"service": "echo", "method": "x"})
    out = wait_sent(hw, 4)[-1]
    hw.reply(out["body"]["call"], ok=False, error={"code": "locked", "detail": "x", "lock": "y" * 70000})
    assert f.result(10).json()["data"] == {}


# managed folders the agent may edit, and walking/removing in them
FOLDERY = '''
def setup(ext):
    @ext.router.get("/walk/{folder}")
    def walk(folder: str):
        return ext.folders.walk(folder)

    @ext.router.post("/remove")
    def remove(body: dict):
        return {"removed": ext.folders.remove(body["folder"], body["path"])}

    @ext.router.post("/write")
    def write(body: dict):
        ext.folders.write(body["folder"], body["path"], body["text"])
        return {}

    @ext.router.get("/read")
    def read(folder: str, path: str):
        return {"text": ext.folders.read(folder, path).decode()}
'''


@pytest.fixture
def foldery(client, exts):
    d = exts / "foldery"
    d.mkdir()
    (d / "extension.toml").write_text('name = "foldery"\ntitle = "Foldery"\nversion = "1"\nsdk = "1.2"\nentry = "extension.py"\n'
                                      'folders = ["plain", {name = "spaces", agent_writable = true}, {name = "ro", agent_writable = false}]\n')
    (d / "extension.py").write_text(FOLDERY)
    ok(client.post("/api/extensions/rescan"))
    ok(client.put("/api/extensions/foldery/enabled", json={"enabled": True}))
    return client


def test_manifest_folders_take_names_or_tables_and_the_prompt_lists_writable_ones_apart(foldery):
    [x] = [e for e in ok(foldery.get("/api/extensions")) if e["name"] == "foldery"]
    assert x["folders"] == ["plain", "spaces", "ro"]  # the owner API still lists names
    m = foldery.be.ext.found["foldery"]
    assert m.folders == ["plain", "spaces", "ro"] and m.writable_folders == ["spaces"]
    system = foldery.be.ext.prompt("system")
    assert "Folders you may edit" in system and "/external/foldery/spaces" in system
    edit, rest = system.split("Folders you may edit", 1)[1].split("Folders it manages", 1)
    assert "/external/foldery/spaces" in edit and "/external/foldery/plain" not in edit and "/external/foldery/ro" not in edit
    assert "/external/foldery/plain" in rest and "/external/foldery/ro" in rest and "/external/foldery/spaces" not in rest


def test_a_plain_folder_list_keeps_the_old_prompt_text(client):
    ok(client.put("/api/extensions/plantlog/enabled", json={"enabled": True}))
    system = client.be.ext.prompt("system")
    assert "Folders it manages (it may write them" in system and "Folders you may edit" not in system


@pytest.mark.parametrize("folders", ['[{name = "x", agent_writable = "yes"}]', '[{name = "x", other = 1}]', '[{agent_writable = true}]', '[5]',
                                     '["a", "a"]', '[{name = "Bad-Name"}]', '"x"', '[{name = "x"}, "x"]'])
def test_bad_folder_entries_are_a_manifest_error(client, exts, folders):
    from l0w_control.extensions import ManifestError, parse_manifest

    d = exts / "badfolders"
    d.mkdir()
    (d / "extension.toml").write_text(f'name = "badfolders"\nversion = "1"\nsdk = "1.2"\nfolders = {folders}\n')
    with pytest.raises(ManifestError, match="folders"):
        parse_manifest(d)


def test_folders_walk_lists_everything_and_never_follows_symlinks(foldery, tmp_path):
    root = tmp_path / "work" / "_inbox" / "_ext" / "foldery" / "spaces"
    assert ok(foldery.get("/api/ext/foldery/walk/spaces")) == []  # not created yet
    ok(foldery.post("/api/ext/foldery/write", json={"folder": "spaces", "path": "a.txt", "text": "hello"}))
    ok(foldery.post("/api/ext/foldery/write", json={"folder": "spaces", "path": "sub/deep/b.md", "text": "hi"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, root / "linkdir")
    os.symlink(outside / "secret.txt", root / "linkfile")
    os.mkfifo(root / "pipe")
    got = ok(foldery.get("/api/ext/foldery/walk/spaces"))
    by = {e["path"]: e for e in got}
    assert [e["path"] for e in got] == sorted(by)  # sorted, a folder before its contents
    assert set(by) == {"a.txt", "sub", "sub/deep", "sub/deep/b.md", "linkdir", "linkfile", "pipe"}
    assert (by["a.txt"]["kind"], by["a.txt"]["size"]) == ("file", 5) and by["a.txt"]["mtime"] > 0
    assert by["sub"]["kind"] == by["sub/deep"]["kind"] == "dir" and by["sub/deep/b.md"]["size"] == 2
    assert by["linkdir"]["kind"] == by["linkfile"]["kind"] == "symlink" and by["pipe"]["kind"] == "other"
    assert not any(p.startswith("linkdir/") for p in by)  # never descended into the link
    assert set(by["a.txt"]) == {"path", "size", "mtime", "kind"}


def test_folders_walk_refuses_a_folder_that_is_a_symlink_and_undeclared_folders(foldery, tmp_path):
    base = tmp_path / "work" / "_inbox" / "_ext" / "foldery"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("x")
    os.symlink(outside, base / "plain")
    r = foldery.get("/api/ext/foldery/walk/plain")
    assert r.status_code == 400 and "plain folder" in r.json()["detail"]
    r = foldery.get("/api/ext/foldery/walk/nope")
    assert r.status_code == 400 and "isn't declared" in r.json()["detail"]


def test_folders_remove_takes_files_links_and_trees_but_never_what_a_link_points_at(foldery, tmp_path):
    root = tmp_path / "work" / "_inbox" / "_ext" / "foldery" / "spaces"
    ok(foldery.post("/api/ext/foldery/write", json={"folder": "spaces", "path": "a.txt", "text": "hello"}))
    ok(foldery.post("/api/ext/foldery/write", json={"folder": "spaces", "path": "tree/x/y.txt", "text": "y"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, root / "tree" / "escape")
    os.symlink(outside / "secret.txt", root / "filelink")

    def rm(path, folder="spaces"):
        return ok(foldery.post("/api/ext/foldery/remove", json={"folder": folder, "path": path}))["removed"]

    assert rm("a.txt") is True and not (root / "a.txt").exists()
    assert rm("a.txt") is False  # already gone
    assert rm("filelink") is True and not os.path.lexists(root / "filelink") and (outside / "secret.txt").read_text() == "secret"
    assert rm("tree") is True and not (root / "tree").exists()  # a tree, including the link inside it
    assert (outside / "secret.txt").read_text() == "secret"  # and nothing outside was touched
    # a link to a folder is removed as a link
    os.symlink(outside, root / "dirlink")
    assert rm("dirlink") is True and not os.path.lexists(root / "dirlink") and outside.is_dir()
    # a path that goes through a link, or up, is refused or finds nothing
    os.symlink(outside, root / "via")
    r = foldery.post("/api/ext/foldery/remove", json={"folder": "spaces", "path": "via/secret.txt"})
    assert r.status_code == 400 and (outside / "secret.txt").exists()
    assert foldery.post("/api/ext/foldery/remove", json={"folder": "spaces", "path": "../plain/x"}).status_code == 400
    assert (outside / "secret.txt").exists()
    # the folder itself stays; undeclared folders are refused
    assert foldery.post("/api/ext/foldery/remove", json={"folder": "spaces", "path": ""}).status_code == 400
    assert foldery.post("/api/ext/foldery/remove", json={"folder": "other", "path": "x"}).status_code == 400
    assert root.is_dir()


def test_reading_what_the_agent_wrote_stays_symlink_safe(foldery, tmp_path):
    root = tmp_path / "work" / "_inbox" / "_ext" / "foldery" / "spaces"
    ok(foldery.post("/api/ext/foldery/write", json={"folder": "spaces", "path": "ok.txt", "text": "fine"}))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside / "secret.txt", root / "evil.txt")
    os.symlink(outside, root / "evildir")
    assert ok(foldery.get("/api/ext/foldery/read", params={"folder": "spaces", "path": "ok.txt"})) == {"text": "fine"}
    for path in ("evil.txt", "evildir/secret.txt"):
        r = foldery.get("/api/ext/foldery/read", params={"folder": "spaces", "path": path})
        assert r.status_code >= 400 and "secret" not in r.text


@pytest.mark.parametrize("route", ["member-keys", "member-keys-sync"])
def test_hub_sdk_gives_a_members_pinned_keys(spacey, route):
    hw = spacey
    r = ok(hw.c.post(f"/api/ext/spacey/{route}", json={"address": CREATOR.upper()}))
    assert r["ok"] is True and r["keys"]["address"] == CREATOR
    assert r["keys"]["sign"] == hw.creator.key.to_dict() and r["keys"]["seal"] == hw.gw.seal_keys[CREATOR].key.to_dict()
    assert hw.gw.bodies("/v0/peer/pin")[-1] == {"address": CREATOR}
    assert ok(hw.c.post(f"/api/ext/spacey/{route}", json={"address": "nobody"}))["code"] == "bad_args"


def test_hub_sdk_gives_this_instances_own_keys_or_says_not_joined(spacey):
    from l0wproto import docs as D

    hw = spacey
    assert ok(hw.c.post("/api/ext/spacey/my-keys", json={}))["code"] == "not_joined"  # no self-description published yet
    payload = {"v": "l0w/0.1", "type": "self", "iat": "2026-11-22T10:00:00Z", "keys": [hw.gw.me_sign.key.to_dict(), hw.gw.me_seal.key.to_dict()]}
    hw.be.db.ex("UPDATE hub_membership SET self_doc=? WHERE id=1", (json.dumps(D.sign_document("self", payload, [hw.gw.me_sign])),))
    r = ok(hw.c.post("/api/ext/spacey/my-keys", json={}))
    assert r == {"ok": True, "keys": {"address": ME, "sign": hw.gw.me_sign.key.to_dict(), "seal": hw.gw.me_seal.key.to_dict()}}


def test_vault_contains_secret_never_names_the_secret(spacey):
    hw = spacey
    ok(hw.c.put("/api/secrets/PLANT_TOKEN", json={"value": "s3cr3t-plant-value-123"}))
    assert ok(hw.c.post("/api/ext/spacey/has-secret", json={"text": "config: s3cr3t-plant-value-123"})) == {"text": True, "bytes": True}
    assert ok(hw.c.post("/api/ext/spacey/has-secret", json={"text": "nothing here"})) == {"text": False, "bytes": False}


def test_hub_sdk_signs_a_space_key_this_instance_grants(spacey):
    hw = spacey
    member = keys.generate("seal")
    sealed = spaces.seal_space_key(SPACE_KEY, member.key, space=SPACE, epoch=1, member=CREATOR, member_kid=member.kid, granter=ME, granter_kid=hw.gw.me_sign.kid)
    p = spaces.build_space_key(SPACE, 1, CREATOR, member.kid, ME, hw.gw.me_sign.kid, sealed)
    r = ok(hw.c.post("/api/ext/spacey/sign-key", json={"payload": p}))
    assert r["ok"] is True
    doc = json.loads(r["doc"])
    spaces.verify_space_key(doc, hw.gw.me_sign.key, member=CREATOR, granter=ME)
    assert spaces.open_space_key_with(member.private, doc) == SPACE_KEY
    other = spaces.build_space_key(SPACE, 1, CREATOR, member.kid, CREATOR, hw.gw.me_sign.kid, sealed)
    assert ok(hw.c.post("/api/ext/spacey/sign-key", json={"payload": other}))["code"] == "refused"
    assert ok(hw.c.post("/api/ext/spacey/sign-key", json={"payload": {"space": SPACE}}))["code"] == "bad_args"
