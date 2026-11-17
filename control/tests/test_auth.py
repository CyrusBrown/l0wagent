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
              agent_home=tmp_path / "agent-home", agent_api_any_address=False)
    return SqliteBackend(env=env)


def client(be, ip=("192.168.1.20", 50000), ua="Mozilla/5.0 (Macintosh; Mac OS X) Firefox/140.0") -> TestClient:
    return TestClient(create_app(be), client=ip, headers={"user-agent": ua, "x-l0w-client": "test"})


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


def test_no_account_lockout(be):
    with client(be) as c:
        setup(c)
        codes = [login(c, "wrong password!").status_code for _ in range(8)]
        assert codes == [401] * 8
        assert login(c).status_code == 200  # the right password still works


def test_rate_limit_per_address(be):
    with client(be) as c:
        setup(c)
        for _ in range(10):
            c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        r = login(c)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    with client(be, ip=("192.168.1.99", 50000)) as other:
        assert login(other).status_code == 200


def test_rate_limit_runs_from_the_newest_failure(be):
    from datetime import timedelta

    from l0w_control.auth import iso, utcnow

    with client(be) as c:
        setup(c)
        now = utcnow()
        for minutes in range(14, 4, -1):  # ten failures spread over 14 minutes
            be.db.ex("INSERT INTO login_attempts(ip, username, ok, at) VALUES(?,?,0,?)", ("192.168.1.20", "x", iso(now - timedelta(minutes=minutes))))
        r = login(c)
        assert r.status_code == 429
        # The newest failure was 5 minutes ago: about 10 minutes left, not the 1 minute the oldest would give.
        assert 9 * 60 <= r.json()["retry_after_s"] <= 10 * 60 + 1


def test_global_slowdown_spares_known_addresses(be):
    from l0w_control.auth import SLOWDOWN_AFTER

    with client(be, ip=("192.168.1.50", 1)) as owner:
        setup(owner)
        assert login(owner).status_code == 200  # this address has now signed in
    for i in range(SLOWDOWN_AFTER):
        be.db.ex("INSERT INTO login_attempts(ip, username, ok, at) VALUES(?,?,0,?)", (f"203.0.113.{i}", "x", be.db.now()))
    with client(be, ip=("198.51.100.9", 1)) as stranger:
        assert login(stranger, "wrong password!").status_code == 401  # one check goes through
        r = login(stranger, "wrong password!")
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"  # the next one has to wait
    with client(be, ip=("192.168.1.50", 2)) as owner2:
        assert [login(owner2).status_code for _ in range(3)] == [200] * 3


def test_totp(be):
    with client(be) as c:
        setup(c)
        login(c)
        assert c.post("/api/account/totp/enable", json={"code": "000000"}).status_code == 400  # no setup yet
        setup_ = c.post("/api/account/totp/setup").json()
        assert setup_["qr_svg"].startswith("<svg") and "otpauth://totp/" in setup_["otpauth_uri"]
        # Kept server-side, sealed.
        assert setup_["secret"] not in be.db.q1("SELECT value FROM kv WHERE key='totp_pending'")["value"]
        assert c.post("/api/account/totp/enable", json={"secret": setup_["secret"], "code": "000000"}).status_code == 400
        step = int(time.time() // 30)
        # A secret the client picks is ignored: only the issued one verifies.
        other = "JBSWY3DPEHPK3PXP"
        assert c.post("/api/account/totp/enable", json={"secret": other, "code": totp_code(other, step)}).status_code == 400
        enabled = c.post("/api/account/totp/enable", json={"secret": other, "code": totp_code(setup_["secret"], step)}).json()
        assert enabled["totp_enabled"] and len(enabled["recovery_codes"]) == 10
        stored = be.db.q1("SELECT totp_secret FROM users")["totp_secret"]
        assert setup_["secret"] not in stored and stored.startswith("fernet:")
        assert be.db.q1("SELECT COUNT(*) AS n FROM recovery_codes")["n"] == 10
        recovery = enabled["recovery_codes"][0]
    with client(be) as c2:
        r = login(c2).json()
        assert r["status"] == "totp_required" and r["challenge"]
        assert c2.get("/api/auth/me").status_code == 401
        # The code used to enable 2FA can't be replayed; the next one works.
        assert c2.post("/api/auth/totp", json={"challenge": r["challenge"], "code": totp_code(setup_["secret"], step)}).status_code == 401
        assert c2.post("/api/auth/totp", json={"challenge": r["challenge"], "code": totp_code(setup_["secret"], step + 1)}).json()["status"] == "ok"
        assert c2.get("/api/auth/me").status_code == 200
    with client(be) as c3:  # a recovery code stands in for the code, once
        r = login(c3).json()
        assert c3.post("/api/auth/totp", json={"challenge": r["challenge"], "code": recovery}).json()["status"] == "ok"
        r = login(c3).json()
        assert c3.post("/api/auth/totp", json={"challenge": r["challenge"], "code": recovery}).status_code == 401
        assert c3.post("/api/account/totp/disable", json={"password": "nope nope nope"}).status_code == 400
        assert c3.post("/api/account/totp/disable", json={"password": PW}).json()["totp_enabled"] is False
        assert be.db.q1("SELECT COUNT(*) AS n FROM recovery_codes")["n"] == 0


def test_plaintext_totp_secret_still_works_and_is_sealed(be):
    with client(be) as c:
        setup(c)
        secret = "JBSWY3DPEHPK3PXP"
        be.db.ex("UPDATE users SET totp_secret=? WHERE id=1", (secret,))
        r = login(c).json()
        step = int(time.time() // 30)
        assert c.post("/api/auth/totp", json={"challenge": r["challenge"], "code": totp_code(secret, step)}).json()["status"] == "ok"
        assert be.db.q1("SELECT totp_secret FROM users")["totp_secret"].startswith("fernet:")


def test_setup_code(tmp_path):
    env = Env(data_dir=tmp_path, setup_code="abc123", runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9", agent_home=tmp_path / "ah")
    be = SqliteBackend(env=env)
    body = {"username": "mara", "password": PW, "timezone": "UTC"}
    with client(be, ip=("8.8.8.8", 1)) as c:
        assert c.post("/api/setup/owner", json=body).status_code == 403
        assert c.post("/api/setup/owner", json={**body, "setup_code": "nope"}).status_code == 403
        assert c.post("/api/setup/owner", json={**body, "setup_code": "abc123"}).status_code == 200


def test_setup_without_code_is_local_only(be):
    body = {"username": "mara", "password": PW, "timezone": "UTC"}
    with client(be, ip=("8.8.8.8", 1)) as far:
        assert far.post("/api/setup/owner", json=body).status_code == 403
    with client(be, ip=("fd00::5", 1)) as near:
        assert near.post("/api/setup/owner", json=body).status_code == 200


def test_proxy_headers_only_from_trusted_proxies(monkeypatch):
    from starlette.requests import Request

    from l0w_control.netaddr import client_ip, request_scheme

    def req(peer, **headers):
        raw = [(k.lower().replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "client": (peer, 1), "scheme": "http", "path": "/", "query_string": b"", "server": ("x", 80), "headers": raw})

    hdrs = {"X_Forwarded_For": "6.6.6.6, 203.0.113.9, 10.0.0.2", "X_Forwarded_Proto": "https"}
    assert client_ip(req("10.0.0.2", **hdrs)) == "10.0.0.2" and request_scheme(req("10.0.0.2", **hdrs)) == "http"
    monkeypatch.setenv("L0W_TRUSTED_PROXIES", "10.0.0.0/24, 172.16.0.1")
    assert client_ip(req("10.0.0.2", **hdrs)) == "203.0.113.9" and request_scheme(req("10.0.0.2", **hdrs)) == "https"
    assert client_ip(req("10.0.0.2", **hdrs, CF_Connecting_IP="198.51.100.4")) == "198.51.100.4"
    assert client_ip(req("8.8.8.8", **hdrs)) == "8.8.8.8" and request_scheme(req("8.8.8.8", **hdrs)) == "http"


def test_session_cookie_secure_modes(be, monkeypatch):
    creds = {"username": "mara", "password": PW}
    with client(be) as c:
        setup(c)
        cookie = c.post("/api/auth/login", json=creds).headers["set-cookie"]
        assert cookie.startswith("l0w_session=") and "Secure" not in cookie and "Max-Age=2592000" in cookie
    monkeypatch.setenv("L0W_TRUSTED_PROXIES", "192.168.1.20")
    with client(be) as c:
        r = c.post("/api/auth/login", json=creds, headers={"x-forwarded-proto": "https"})
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-l0w_session=") and "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
        token = r.cookies.get("__Host-l0w_session")
        # Either cookie name authenticates.
        assert c.get("/api/auth/me", cookies={"__Host-l0w_session": token}).status_code == 200
    with client(be) as c:  # not over HTTPS from the proxy's view: plain cookie
        assert c.post("/api/auth/login", json=creds).headers["set-cookie"].startswith("l0w_session=")
    be.env = Env(**{**be.env.__dict__, "cookie_secure": "never"})
    with client(be) as c:
        assert "Secure" not in c.post("/api/auth/login", json=creds, headers={"x-forwarded-proto": "https"}).headers["set-cookie"]
    be.env = Env(**{**be.env.__dict__, "cookie_secure": "always"})
    with client(be) as c:
        assert c.post("/api/auth/login", json=creds).headers["set-cookie"].startswith("__Host-l0w_session=")


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
    with client(be) as lan:
        # The token is useless from outside the sandbox subnet.
        r = lan.get("/api/agent/ping", headers={"X-Agent-Token": "agent-secret"})
        assert r.status_code == 403 and r.json()["error"] == "agent_api_from_sandbox_only"
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


def test_cookie_writes_need_the_client_header(be):
    """A page on another port of the same host (the agent's web page) gets the session cookie
    too; without the custom header its requests can't change anything."""
    with TestClient(create_app(be), client=("192.168.1.20", 5000)) as c:
        setup(c)
        assert login(c).status_code == 200  # no cookie yet: allowed
        assert "l0w_session" in c.cookies
        r = c.post("/api/runs", json={})
        assert r.status_code == 403 and r.json()["error"] == "csrf"
        assert c.post("/api/threads", json={"title": "x"}).status_code == 403
        assert c.get("/api/auth/me").status_code == 200  # reads are fine
        assert c.post("/api/threads", json={"title": "x"}, headers={"x-l0w-client": "web"}).status_code == 201
