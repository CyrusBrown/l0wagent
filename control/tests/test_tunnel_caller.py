"""The hub tunnel's caller class: what requests through the tunnel may reach, and who they come from."""

import ipaddress

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env
from l0w_control.guard import OTHER, SANDBOX, TUNNEL, allowed, caller_class

SANDBOX_NET = ipaddress.ip_network("10.255.255.0/24")
TUNNEL_NET = ipaddress.ip_network("10.255.254.0/24")
TUNNEL_IP = ("10.255.254.9", 40000)
PW = "correct horse battery"


@pytest.mark.parametrize("host,cls", [("10.255.254.9", TUNNEL), ("10.255.255.9", SANDBOX), ("192.168.1.20", OTHER), ("fd00::1", OTHER)])
def test_class(host, cls):
    assert caller_class(host, SANDBOX_NET, None, TUNNEL_NET) == cls


@pytest.mark.parametrize("path,ok", [
    ("/", True), ("/inbox", True), ("/api/runs", True), ("/mcp", True), ("/oauth/authorize", True), ("/.well-known/oauth-authorization-server", True),
    ("/api/peer/inbox", True), ("/api/hub/ping", True), ("/s/demo/", True),
    ("/api/agent/ping", False), ("/api/agent", False), ("/api/chat-runtime/mcp", False),
])
def test_rules(path, ok):
    # The dev escape hatches never widen the tunnel class.
    assert allowed(TUNNEL, path) is ok
    assert allowed(TUNNEL, path, agent_any_address=True, chat_net_set=False) is ok


@pytest.fixture
def be(tmp_path, monkeypatch):
    monkeypatch.setenv("L0W_TUNNEL_SUBNET", str(TUNNEL_NET))
    env = Env(data_dir=tmp_path, agent_token="agent-secret", sandbox_subnet=str(SANDBOX_NET), tunnel_subnet=str(TUNNEL_NET),
              runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home", agent_api_any_address=True)
    return SqliteBackend(env=env)


def test_through_the_tunnel(be):
    with TestClient(create_app(be), client=TUNNEL_IP, headers={"x-l0w-client": "test", "x-l0w-tunnel-client": "127.0.0.1"}) as c:
        # Never local, whatever the tunnel header says: no setup without the setup code.
        r = c.post("/api/setup/owner", json={"username": "mara", "password": PW, "display_name": "Mara", "timezone": "UTC"})
        assert r.status_code == 403
        # The agent token is useless through the tunnel, even with the dev escape hatch on.
        r = c.get("/api/agent/ping", headers={"x-agent-token": "agent-secret"})
        assert r.status_code == 403 and r.json()["error"] == "agent_api_from_sandbox_only"
        assert c.get("/api/meta").status_code == 200
        assert c.get("/api/peer/x").status_code == 404  # nothing there (the inbox is POST /api/peer/v0/inbox: tests/test_friends.py)


def test_tunnel_client_address_and_scheme(monkeypatch):
    from starlette.requests import Request

    from l0w_control.netaddr import client_ip, is_lan_caller, request_scheme

    monkeypatch.setenv("L0W_TUNNEL_SUBNET", str(TUNNEL_NET))
    monkeypatch.setenv("L0W_TRUSTED_PROXIES", str(TUNNEL_NET))  # the tunnel header wins over forwarding headers

    def req(peer, headers):
        return Request({"type": "http", "client": peer, "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                        "scheme": "http", "path": "/", "server": ("x", 80), "method": "GET", "query_string": b""})

    r = req(TUNNEL_IP, {"x-l0w-tunnel-client": "203.0.113.7", "x-forwarded-for": "198.51.100.1", "x-forwarded-proto": "http"})
    assert client_ip(r) == "203.0.113.7" and request_scheme(r) == "https" and not is_lan_caller(r)
    assert client_ip(req(TUNNEL_IP, {})) == "unknown" and not is_lan_caller(req(TUNNEL_IP, {"x-l0w-tunnel-client": "10.0.0.2"}))
    # Off the tunnel network the header means nothing.
    r = req(("192.168.1.20", 5), {"x-l0w-tunnel-client": "203.0.113.7"})
    assert client_ip(r) == "192.168.1.20" and is_lan_caller(r)
