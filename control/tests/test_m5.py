"""M5: the l0w CLI against a running stack (device token), draining for updates, the update
check, and control serving the built web app."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from l0w_control.updates import _ver

from .stack import PASSWORD, REPO, Stack

CLI = REPO / "cli" / "l0w.py"


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path, steps=3, delay=0.2).up()
    s.login()
    yield s
    s.down()


def l0w(stack: Stack, token: str, *args: str, stdin: str | None = None) -> str:
    env = {**os.environ, "L0W_URL": stack.url, "L0W_TOKEN": token, "XDG_CONFIG_HOME": str(stack.tmp / "xdg")}
    r = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, input=stdin, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout


def test_the_cli_talks_to_the_server_with_a_device_token(stack: Stack):
    r = stack.client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD, "want": "device_token", "device_label": "l0w CLI on test"})
    token = r.json()["device_token"]
    assert token
    assert "Dormant" in l0w(stack, token, "status") or "Asleep" in l0w(stack, token, "status")
    out = l0w(stack, token, "send", "--new", "Garden", "Check the pump logs")
    assert "Started thread #" in out
    assert "Garden" in l0w(stack, token, "threads")
    assert "Check the pump logs" in l0w(stack, token, "read", "1")
    assert "Level set to busy (400 pts a week)" in l0w(stack, token, "level", "busy")
    assert "400 pts left" in l0w(stack, token, "budget")
    l0w(stack, token, "secret", "set", "GARDEN_TOKEN", "-", "--desc", "for the pumps", stdin="s3cret-value-123\n")
    assert "GARDEN_TOKEN" in l0w(stack, token, "secrets")
    assert "New sign-in: l0w CLI on test" in l0w(stack, token, "inbox")  # every new device raises a card
    assert "Nothing this week." in l0w(stack, token, "calendar") or "pts" in l0w(stack, token, "calendar")
    # A device token is a session the owner can see and revoke.
    sessions = stack.client.get("/api/account/sessions").json()
    assert any(s["label"] == "l0w CLI on test" and s["kind"] == "device" for s in sessions)


def test_draining_holds_new_runs_until_turned_off(stack: Stack):
    env = {**os.environ, "L0W_DATA_DIR": str(stack.tmp / "control")}

    def hostctl(*args):
        r = subprocess.run([sys.executable, "-m", "l0w_control.hostctl", *args], capture_output=True, text=True, env=env, cwd=REPO / "control")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    assert hostctl("idle") == {"idle": True, "waiting_for": []}
    assert hostctl("drain", "on", "30")["draining_until"]
    r = stack.client.post("/api/runs", json={})
    assert r.status_code == 409 and r.json()["error"] == "updating"
    assert stack.client.get("/api/system/update").json()["draining_until"]
    hostctl("drain", "off")
    run = stack.client.post("/api/runs", json={}).json()
    assert run["id"]
    assert not hostctl("idle")["idle"] and "in progress" in hostctl("idle")["waiting_for"][0]
    stack.wait_for(lambda: stack.run(run["id"])["status"] not in ("starting", "running"), timeout=60, what="run to end")


def test_stale_drain_is_cleared_and_renewing_keeps_it(stack: Stack):
    from datetime import timedelta

    from l0w_control.db import Database
    from l0w_control.updates import draining, set_drain

    db = Database(str(stack.tmp / "control" / "control.db"))
    set_drain(db, True, 15, by="l0w update")
    assert draining(db)
    old = (datetime.now(UTC) - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.ex("UPDATE kv SET updated_at=? WHERE key='drain_until'", (old,))
    assert draining(db) is None  # nobody renewed it: the updater died
    assert db.q1("SELECT 1 FROM kv WHERE key IN ('drain_until','drain_by')") is None
    set_drain(db, True, 15)
    db.ex("UPDATE kv SET updated_at=? WHERE key='drain_until'", (old,))
    set_drain(db, True, 15)  # a renewal
    assert draining(db)
    set_drain(db, False)


def test_cancel_route_clears_the_drain(stack: Stack):
    from l0w_control.db import Database
    from l0w_control.updates import set_drain

    set_drain(Database(str(stack.tmp / "control" / "control.db")), True, 30)
    assert stack.client.get("/api/system/update").json()["draining_until"]
    r = stack.client.post("/api/system/update/cancel")
    assert r.status_code == 200 and r.json()["draining_until"] is None
    assert stack.client.post("/api/runs", json={}).status_code != 409


def test_versions_compare():
    assert _ver("0.10.0") > _ver("v0.9.3") and _ver("edge-abc123") is None


def test_control_serves_the_web_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from l0w_control.app import create_app
    from l0w_control.mock.backend import MockBackend

    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>l0wagent</title>")
    (web / "assets" / "app-1234.js").write_text("console.log(1)")
    (web / "sw.js").write_text("self.addEventListener('fetch', () => {})")
    monkeypatch.setenv("L0W_WEB_DIR", str(web))
    with TestClient(create_app(MockBackend())) as c:
        assert "l0wagent" in c.get("/t/12").text  # the router's paths get the app
        a = c.get("/assets/app-1234.js")
        assert a.status_code == 200 and "immutable" in a.headers["cache-control"]
        assert c.get("/sw.js").headers["cache-control"] == "no-cache"
        assert c.get("/api/nope").status_code == 404
        assert c.get("/../../etc/passwd").text.startswith("<!doctype")
        assert c.get("/api/meta").json()["api_version"]
    assert Path(web / "index.html").exists()


def test_notifications_follow_the_owners_settings(tmp_path, monkeypatch):
    import asyncio
    import base64

    import pywebpush

    from l0w_control.api import schemas as s
    from l0w_control.db import Database
    from l0w_control.notify import Notifier

    db = Database(str(tmp_path / "c.db"))
    n = Notifier(db, tmp_path)
    assert len(base64.urlsafe_b64decode(n.public_key() + "==")) == 65  # an uncompressed P-256 point
    assert Notifier(db, tmp_path).public_key() == n.public_key()  # kept on disk
    sent = []

    class Gone(pywebpush.WebPushException):
        pass

    def fake_webpush(subscription_info, data, **kw):
        if "gone" in subscription_info["endpoint"]:
            resp = type("R", (), {"status_code": 410})()
            raise Gone("expired", response=resp)
        sent.append((subscription_info["endpoint"], json.loads(data)))

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    for ep in ("https://push.example/phone", "https://push.example/gone"):
        n.subscribe(s.PushSubscribe(endpoint=ep, keys=s.PushKeys(p256dh="p", auth="a"), label="Phone"))
    loop = asyncio.new_event_loop()
    n.loop = loop

    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        n.notify("approval", "Approval needed", "Allow api.github.com", "/t/3")
        n.notify("assistant_message", "The assistant wrote", "hi", "/t/3")  # off by default
        import time as _t
        _t.sleep(0.5)
        assert [p["title"] for _, p in sent] == ["Approval needed"] and sent[0][1]["url"] == "/t/3"
        assert [x.label for x in n.settings().subscriptions] == ["Phone"]  # the expired one is dropped
        st = n.settings()
        st.events["assistant_message"] = True
        st.quiet_start, st.quiet_end = "00:00", "23:59"
        n.save_settings(st)
        n.notify("assistant_message", "Quiet", "", "/")   # held back by quiet hours
        n.notify("new_device", "New sign-in", "", "/")      # never held back
        _t.sleep(0.5)
        assert [p["title"] for _, p in sent] == ["Approval needed", "New sign-in"]
        assert db.q1("SELECT COUNT(*) AS c FROM notifications")["c"] == 4  # all recorded
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_logging_the_agent_in_through_the_server(stack: Stack):
    r = stack.client.post("/api/providers/claude-subscription/login")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://claude.ai/oauth/authorize")
    assert stack.lease_active()  # the model API is open for the login only
    bad = stack.client.post("/api/providers/claude-subscription/login/code", json={"code": "nope-code"}).json()
    assert not bad["ok"] and not stack.lease_active()
    stack.client.post("/api/providers/claude-subscription/login")
    ok = stack.client.post("/api/providers/claude-subscription/login/code", json={"code": "good-code"}).json()
    assert ok["ok"], ok
    assert stack.client.get("/api/system").json()["runner"]["logged_in"]


def test_ntfy_title_only_token_and_vetted_url(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from l0w_control import safefetch
    from l0w_control.api import schemas as s
    from l0w_control.backends.base import BadRequest
    from l0w_control.db import Database
    from l0w_control.notify import Notifier
    from l0w_control.vault import Vault

    known = {"control": ["172.20.0.2"], "ntfy.lan": ["192.168.1.9"]}
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: known.get(host, [] if host in safefetch.STACK_NAMES else ["93.184.216.34"]))
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))
    db = Database(str(tmp_path / "c.db"))
    n = Notifier(db, tmp_path)
    n.vault = Vault(db, tmp_path)
    posts = []
    n.ntfy_transport = httpx.MockTransport(lambda r: (posts.append(r), httpx.Response(200))[1])

    def save(**kw):
        return n.save_settings(s.NotificationSettings(events={}, **kw))

    for bad in ("https://control/topic", "http://localhost/t", "https://127.0.0.1/t", "http://ntfy.example.com/t"):
        with pytest.raises(BadRequest):
            save(ntfy_url=bad)
    st = save(ntfy_url="http://ntfy.lan/topic", ntfy_token="tk_secretsecret1")  # a LAN server is fine over http
    assert st.ntfy_token_saved and st.ntfy_token is None and not st.ntfy_previews
    assert "tk_secretsecret1" not in st.model_dump_json()
    payload = {"title": "Approval needed", "body": "Allow api.github.com to read your repo", "url": "/t/3"}
    assert asyncio.run(n._send(1, payload, st.ntfy_url, st.ntfy_previews)) == 1
    assert posts[0].content == b"Approval needed" and posts[0].headers["authorization"] == "Bearer tk_secretsecret1"
    st = save(ntfy_url="http://ntfy.lan/topic", ntfy_previews=True)  # token omitted: kept
    assert st.ntfy_token_saved and st.ntfy_previews
    asyncio.run(n._send(2, payload, st.ntfy_url, st.ntfy_previews))
    assert posts[1].content == b"Allow api.github.com to read your repo"
    st = save(ntfy_url="http://ntfy.lan/topic", ntfy_token="")  # empty removes it
    assert not st.ntfy_token_saved


def test_2fa_reset_turns_off_totp_and_deletes_recovery_codes(stack: Stack):
    from l0w_control.auth import totp_code

    c = stack.client
    secret = c.post("/api/account/totp/setup").json()["secret"]
    enabled = c.post("/api/account/totp/enable", json={"code": totp_code(secret, int(__import__("time").time() // 30))})
    assert enabled.json()["totp_enabled"] and len(enabled.json()["recovery_codes"]) == 10
    env = {**os.environ, "L0W_DATA_DIR": str(stack.tmp / "control")}
    r = subprocess.run([sys.executable, "-m", "l0w_control.hostctl", "2fa-reset"], capture_output=True, text=True, env=env, cwd=REPO / "control")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"totp_was_enabled": True, "recovery_codes_deleted": 10}
    assert c.get("/api/auth/me").json()["totp_enabled"] is False
    # Run again: nothing left to reset.
    r = subprocess.run([sys.executable, "-m", "l0w_control.hostctl", "2fa-reset"], capture_output=True, text=True, env=env, cwd=REPO / "control")
    assert json.loads(r.stdout) == {"totp_was_enabled": False, "recovery_codes_deleted": 0}


def test_system_status_reports_storage_and_warns_when_free_space_is_low(stack: Stack):
    st = stack.client.get("/api/system").json()["storage"]
    assert st["disk_free_bytes"] > 0 and st["db_bytes"] > 0 and st["warn_free_gb"] == 5 and isinstance(st["low"], bool)
    assert {v["name"] for v in st["volumes"]} <= {"workspace", "work", "cache"}
    assert next((v for v in st["volumes"] if v["name"] == "cache"), {}).get("bytes") is None


def test_low_disk_notifies_once_a_day(tmp_path):
    import asyncio

    from l0w_control.backends.sqlite import SqliteBackend
    from l0w_control.config import Env

    be = SqliteBackend(env=Env(data_dir=tmp_path, agent_token="t", runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9", agent_home=tmp_path / "ah"))
    be.disk.free = lambda: (1 * 1024**3, 100 * 1024**3)  # type: ignore[method-assign]
    st = be.disk.status(be.settings.get("DISK_WARN_FREE_GB"))
    assert st.low and st.warn_free_gb == 5
    now = datetime(2026, 11, 17, 8, tzinfo=UTC)
    count = lambda: be.db.q1("SELECT COUNT(*) AS n FROM notifications WHERE kind='disk_low'")["n"]  # noqa: E731
    asyncio.run(be._check_disk(now))
    asyncio.run(be._check_disk(now.replace(hour=20)))
    assert count() == 1
    asyncio.run(be._check_disk(now.replace(day=18)))
    assert count() == 2
    be.disk.free = lambda: (50 * 1024**3, 100 * 1024**3)  # type: ignore[method-assign]
    asyncio.run(be._check_disk(now.replace(day=19)))
    assert count() == 2
