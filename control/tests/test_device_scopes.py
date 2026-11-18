"""Scoped device tokens: what full, cli and read tokens may do."""

import pytest

from l0w_control.auth import scope_allows


@pytest.mark.parametrize("scope,method,path,ok", [
    ("full", "POST", "/api/account/password", True),
    ("read", "GET", "/api/runs", True),
    ("read", "POST", "/api/threads", False),
    ("read", "POST", "/api/auth/logout", True),
    ("read", "DELETE", "/api/account/sessions/x", False),
    ("cli", "POST", "/api/threads", True),
    ("cli", "POST", "/api/runs", True),
    ("cli", "GET", "/api/account/sessions", True),
    ("cli", "POST", "/api/account/password", False),
    ("cli", "DELETE", "/api/account/sessions/x", False),
    ("cli", "PUT", "/api/secrets/GITHUB", False),
    ("cli", "POST", "/api/security-alerts/1/decide", False),
    ("cli", "PATCH", "/api/settings/advanced", False),
    ("cli", "DELETE", "/api/connected-apps/a", False),
    ("cli", "POST", "/api/integrations", False),
    ("cli", "POST", "/api/accounting", True),  # a different path
])
def test_scope_table(scope, method, path, ok):
    assert scope_allows(scope, method, path) is ok


def test_scoped_tokens_end_to_end(tmp_path):
    from fastapi.testclient import TestClient

    from l0w_control.app import create_app
    from l0w_control.backends.sqlite import SqliteBackend
    from l0w_control.config import Env

    env = Env(data_dir=tmp_path, agent_token="a", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9",
              agent_home=tmp_path / "agent-home")
    be = SqliteBackend(env=env)
    pw = "correct horse battery"
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": pw, "timezone": "UTC"})
        r = c.post("/api/auth/login", json={"username": "mara", "password": pw, "want": "device_token", "scope": "read", "device_label": "dash"})
        read = r.json()["device_token"]
        c.post("/api/auth/login", json={"username": "mara", "password": pw})
        cli = c.post("/api/account/devices", json={"label": "laptop cli", "scope": "cli"})
        assert cli.status_code == 201
        cli = cli.json()["device_token"]
        scopes = {x["label"]: x["scope"] for x in c.get("/api/account/sessions").json()}
        assert scopes["dash"] == "read" and scopes["laptop cli"] == "cli"
        c.cookies.clear()
        h = lambda t: {"authorization": f"Bearer {t}"}  # noqa: E731
        assert c.get("/api/threads", headers=h(read)).status_code == 200
        assert c.post("/api/threads", json={"title": "x"}, headers=h(read)).status_code == 403
        assert c.post("/api/threads", json={"title": "x"}, headers=h(cli)).status_code == 201
        assert c.post("/api/account/devices", json={"label": "more", "scope": "full"}, headers=h(cli)).status_code == 403
        assert c.put("/api/secrets/X", json={"value": "v"}, headers=h(cli)).status_code == 403
        sid = next(x["id"] for x in c.get("/api/account/sessions", headers=h(cli)).json() if x["label"] == "dash")
        assert c.delete(f"/api/account/sessions/{sid}", headers=h(cli)).status_code == 403
