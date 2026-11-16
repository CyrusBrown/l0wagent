"""M5: the l0w CLI against a running stack (device token), draining for updates, the update
check, and control serving the built web app."""

import json
import os
import subprocess
import sys
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
