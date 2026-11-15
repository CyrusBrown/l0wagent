"""The owner account, sessions, rate limits and the request guard (real backend, no runner)."""

import time

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.auth import totp_code
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

PW = "correct horse battery"
SANDBOX_IP = ("10.255.255.7", 40000)


@pytest.fixture
def be(tmp_path):
    env = Env(data_dir=tmp_path, agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9",
              agent_home=tmp_path / "agent-home")
    return SqliteBackend(env=env)


def client(be, ip=("192.168.1.20", 50000), ua="Mozilla/5.0 (Macintosh; Mac OS X) Firefox/140.0") -> TestClient:
    return TestClient(create_app(be), client=ip, headers={"user-agent": ua})


def setup(c: TestClient) -> None:
    r = c.post("/api/setup/owner", json={"username": "mara", "password": PW, "display_name": "Mara", "timezone": "Europe/Berlin"})
    assert r.status_code == 200, r.text


def login(c: TestClient, password: str = PW, **kw):
    return c.post("/api/auth/login", json={"username": "mara", "password": password, **kw})


def test_setup_then_login(be):
    with client(be) as c:
        assert c.get("/api/meta").json()["setup_complete"] is False
        assert c.get("/api/auth/me").status_code == 401
        assert c.post("/api/setup/owner", json={"username": "mara", "password": "short", "timezone": "UTC"}).status_code == 422
        setup(c)
        r = c.post("/api/setup/owner", json={"username": "x", "password": PW + "!", "timezone": "UTC"})
        assert r.status_code == 409 and r.json()["error"] == "already_set_up"
        assert login(c).json() == {"status": "ok", "challenge": None, "device_token": None}
        me = c.get("/api/auth/me").json()
        assert me["username"] == "mara" and me["totp_enabled"] is False
        sessions = c.get("/api/account/sessions").json()
        assert len(sessions) == 1 and sessions[0]["current"] and sessions[0]["label"] == "Firefox on macOS"
        # Every new sign-in raises an Inbox card.
        inbox = c.get("/api/inbox").json()
        assert [e["kind"] for e in inbox] == ["security"] and "Firefox on macOS" in inbox[0]["security"]["text"]
        c.post("/api/auth/logout")
        assert c.get("/api/auth/me").status_code == 401


def test_device_token(be):
    with client(be) as c:
        setup(c)
        token = login(c, want="device_token", device_label="l0w CLI on mac-mini").json()["device_token"]
        assert token
    with client(be) as c2:
        assert c2.get("/api/auth/me").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert c2.get("/api/auth/me", headers=headers).status_code == 200
        [s] = [s for s in c2.get("/api/account/sessions", headers=headers).json() if s["kind"] == "device"]
        assert s["label"] == "l0w CLI on mac-mini" and s["current"]
        assert c2.delete(f"/api/account/sessions/{s['id']}", headers=headers).status_code == 200
        assert c2.get("/api/auth/me", headers=headers).status_code == 401


def test_lockout_after_consecutive_failures(be):
    with client(be) as c:
        setup(c)
        codes = [login(c, "wrong password!").status_code for _ in range(5)]
        assert codes[:4] == [401] * 4 and codes[4] == 423
        r = login(c)  # even the right password, while locked
        assert r.status_code == 423 and r.json()["error"] == "locked" and int(r.headers["retry-after"]) > 0
        assert be.db.q1("SELECT COUNT(*) AS n FROM notifications WHERE kind='lockout'")["n"] == 1


def test_rate_limit_per_address(be):
    be.settings.set("LOGIN_LOCKOUT_FAILURES", 100)
    with client(be) as c:
        setup(c)
        for _ in range(10):
            c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        r = login(c)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    with client(be, ip=("192.168.1.99", 50000)) as other:
        assert login(other).status_code == 200


def test_totp(be):
    with client(be) as c:
        setup(c)
        login(c)
        setup_ = c.post("/api/account/totp/setup").json()
        assert setup_["qr_svg"].startswith("<svg") and "otpauth://totp/" in setup_["otpauth_uri"]
        assert c.post("/api/account/totp/enable", json={"secret": setup_["secret"], "code": "000000"}).status_code == 400
        step = int(time.time() // 30)
        assert c.post("/api/account/totp/enable", json={"secret": setup_["secret"], "code": totp_code(setup_["secret"], step)}).json()["totp_enabled"]
    with client(be) as c2:
        r = login(c2).json()
        assert r["status"] == "totp_required" and r["challenge"]
        assert c2.get("/api/auth/me").status_code == 401
        # The code used to enable 2FA can't be replayed; the next one works.
        assert c2.post("/api/auth/totp", json={"challenge": r["challenge"], "code": totp_code(setup_["secret"], step)}).status_code == 401
        assert c2.post("/api/auth/totp", json={"challenge": r["challenge"], "code": totp_code(setup_["secret"], step + 1)}).json()["status"] == "ok"
        assert c2.get("/api/auth/me").status_code == 200
        assert c2.post("/api/account/totp/disable", json={"password": "nope nope nope"}).status_code == 400
        assert c2.post("/api/account/totp/disable", json={"password": PW}).json()["totp_enabled"] is False


def test_password_change_signs_out_other_sessions(be):
    with client(be) as a, client(be, ip=("192.168.1.30", 1)) as b:
        setup(a)
        login(a)
        login(b)
        r = a.post("/api/account/password", json={"current_password": "wrong one here", "new_password": "another long password"})
        assert r.status_code == 400 and r.json()["error"] == "wrong_password"
        assert a.post("/api/account/password", json={"current_password": PW, "new_password": "another long password"}).status_code == 200
        assert a.get("/api/auth/me").status_code == 200
        assert b.get("/api/auth/me").status_code == 401
        assert login(a).status_code == 401


def test_guard(be):
    with client(be) as c:
        setup(c)
    with client(be, ip=SANDBOX_IP) as sandbox:
        # The sandbox can't reach the owner API, whatever it sends …
        for path in ("/api/meta", "/api/auth/me", "/api/runs", "/api/setup/owner"):
            assert sandbox.get(path).status_code == 403
        assert sandbox.post("/api/auth/login", json={"username": "mara", "password": PW}).status_code == 403
        # … and only reaches its own API with the agent token.
        assert sandbox.get("/api/agent/ping").status_code == 401
        assert sandbox.get("/api/agent/ping", headers={"X-Agent-Token": "wrong"}).status_code == 401
        assert sandbox.get("/api/agent/ping", headers={"X-Agent-Token": "agent-secret"}).json() == {"ok": True}
        # The agent can't act outside a run.
        r = sandbox.post("/api/agent/notes", json={"text": "hi"}, headers={"X-Agent-Token": "agent-secret"})
        assert r.status_code == 409
    with client(be) as c:
        assert c.get("/api/peer/anything").status_code == 404
        assert c.get("/api/hub/x").json()["error"] == "reserved"


def test_sync_feed(be):
    with client(be) as c:
        setup(c)
        login(c)
        full = c.get("/api/sync").json()
        alerts = [ch for ch in full["changes"] if ch["entity"] == "security_alert"]
        assert len(alerts) == 1 and alerts[0]["data"]["status"] == "open"
        cursor = full["cursor"]
        assert c.get("/api/sync", params={"since": cursor}).json()["changes"] == []
        c.post(f"/api/security-alerts/{alerts[0]['data']['id']}/decide", json={"decision": "confirm"})
        changes = c.get("/api/sync", params={"since": cursor}).json()["changes"]
        assert [(ch["entity"], ch["data"]["status"]) for ch in changes] == [("security_alert", "confirmed")]
        assert c.get("/api/inbox/counts").json()["waiting"] == 0

