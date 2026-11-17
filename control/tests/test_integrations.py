# ruff: noqa: F811
"""Brokered MCP integrations: sync, the agent's MCP endpoint, the ask flow, redaction. Upstream is faked with httpx.MockTransport."""

import json

import httpx
import pytest

from l0w_control import safefetch
from l0w_control.backends.base import BadRequest, Conflict

from .test_conversations import agent, be, c, ok  # noqa: F401

TOKEN = "ghp_0123456789abcdef"
TOOLS = [
    {"name": "get_issue", "description": "Read an issue", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}},
    {"name": "create_issue", "description": "Open an issue", "inputSchema": {"type": "object"}},
    {"name": "delete_repo", "description": "Delete a repo"},
]


class Upstream:
    def __init__(self, sse=False, status=None, leak=None):
        self.sse, self.status, self.leak = sse, status, leak
        self.auth: list[str | None] = []
        self.calls: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.auth.append(req.headers.get("authorization"))
        if self.status:
            return httpx.Response(self.status, text="nope")
        if req.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(req.content)
        m = body.get("method")
        if m == "notifications/initialized":
            return httpx.Response(202)
        if m == "initialize":
            return self._json(body, {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}, {"Mcp-Session-Id": "s1"})
        if m == "tools/list":
            return self._json(body, {"tools": TOOLS}, sse=self.sse)
        if m == "tools/call":
            p = body["params"]
            self.calls.append(p)
            text = f"{p['name']} {json.dumps(p['arguments'], sort_keys=True)}" + (f" {self.leak}" if self.leak else "")
            return self._json(body, {"content": [{"type": "text", "text": text}]})
        return httpx.Response(400)

    def _json(self, body, result, headers=None, sse=False):
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"event: message\ndata: {json.dumps(msg)}\n\n")
        return httpx.Response(200, json=msg, headers=headers)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """example.com names resolve to a public address, the stack's own names to container addresses."""
    names = {"control": "172.20.0.2", "egress-admin": "172.20.0.3", "agent": "172.21.0.4", "nas.lan": "192.168.1.50", "evil.example.org": "172.20.0.2"}
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: [names[host]] if host in names else [] if host in safefetch.STACK_NAMES else ["93.184.216.34"])
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))


def setup(c, be, up, secret=TOKEN, name="gh", allow_reads=True):
    be.integrations.transport = httpx.MockTransport(up)
    if secret:
        ok(c.put("/api/secrets/GH_TOKEN", json={"value": secret, "scope": "integrations"}))
    v = be.integrations.create(name, "GitHub", None, "https://mcp.example.com/mcp", "GH_TOKEN")
    if allow_reads:  # the owner's decision: nothing starts allowed on its own
        be.integrations.set_policy(name, "get_issue", "allowed")
        v = be.integrations.get(name)
    return v


def rpc(c, method, params=None, name="gh", id=1):
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {}), **({"id": id} if id else {})}
    return agent(c, "POST", f"/mcp/{name}", json=body)


def call(c, tool, args, name="gh"):
    r = ok(rpc(c, "tools/call", {"name": tool, "arguments": args}, name))
    return r["result"]


def policies(be):
    return {t["name"]: t["policy"] for t in be.integrations.get("gh")["tools"]}


def test_create_syncs_tools_and_keeps_secret_out_of_env(c, be):
    up = Upstream()
    v = setup(c, be, up, allow_reads=False)
    assert v["status"] == "ok" and v["secret_saved"]
    # The server's readOnlyHint is only a label: nothing starts allowed on a custom integration.
    assert policies(be) == {"get_issue": "ask", "create_issue": "ask", "delete_repo": "ask"}
    assert {t["name"]: t["read_only"] for t in v["tools"]}["get_issue"] is True
    assert set(up.auth) == {f"Bearer {TOKEN}"}
    assert "GH_TOKEN" not in be.vault.env(None)


def test_agent_mcp_endpoint(c, be):
    setup(c, be, Upstream())
    init = ok(rpc(c, "initialize", {"protocolVersion": "2025-06-18"}))
    assert init["result"]["serverInfo"]["name"] == "l0w-gh"
    assert rpc(c, "notifications/initialized", id=0).status_code == 202
    tools = {t["name"]: t for t in ok(rpc(c, "tools/list"))["result"]["tools"]}
    assert set(tools) == {"get_issue", "create_issue", "delete_repo"}
    assert tools["create_issue"]["description"].startswith("[Asks the owner first.]")
    assert not tools["get_issue"]["description"].startswith("[Asks")
    be.integrations.set_policy("gh", "delete_repo", "blocked")
    assert "delete_repo" not in {t["name"] for t in ok(rpc(c, "tools/list"))["result"]["tools"]}
    r = call(c, "delete_repo", {})
    assert r.get("isError") and "isn't available" in r["content"][0]["text"]


def test_allowed_call_forwards_and_logs(c, be):
    up = Upstream()
    setup(c, be, up)
    r = call(c, "get_issue", {"n": 1})
    assert not r.get("isError") and r["content"][0]["text"] == 'get_issue {"n": 1}'
    assert up.calls == [{"name": "get_issue", "arguments": {"n": 1}}]
    assert be.integrations.get("gh")["recent_calls"][0]["outcome"] == "ok"


def test_ask_flow_once(c, be):
    up = Upstream()
    setup(c, be, up)
    r = call(c, "create_issue", {"title": "x"})
    text = r["content"][0]["text"]
    assert r.get("isError") and "Asked as permission #" in text
    pid = int(text.split("#")[1].split(":")[0])
    assert be.conv.permission(pid).scopes == ["once", "always"]
    again = call(c, "create_issue", {"title": "x"})
    assert "Still waiting" in again["content"][0]["text"]
    assert len(be.db.q("SELECT id FROM permissions")) == 1
    assert up.calls == []

    ok(c.post(f"/api/permissions/{pid}/decide", json={"decision": "allow", "scope": "once"}))
    r = call(c, "create_issue", {"title": "x"})
    assert not r.get("isError") and len(up.calls) == 1
    third = call(c, "create_issue", {"title": "x"})
    assert "Asked as permission #" in third["content"][0]["text"] and len(up.calls) == 1
    assert len(be.db.q("SELECT id FROM permissions")) == 2
    other = call(c, "create_issue", {"title": "y"})
    assert "Asked as permission #" in other["content"][0]["text"]
    assert len(be.db.q("SELECT id FROM permissions")) == 3
    assert policies(be)["create_issue"] == "ask"


def test_allow_always_sets_policy(c, be):
    setup(c, be, Upstream())
    text = call(c, "create_issue", {"title": "x"})["content"][0]["text"]
    pid = int(text.split("#")[1].split(":")[0])
    ok(c.post(f"/api/permissions/{pid}/decide", json={"decision": "allow", "scope": "always"}))
    assert policies(be)["create_issue"] == "allowed"
    assert not call(c, "create_issue", {"title": "z"}).get("isError")


def test_agent_cannot_file_mcp_call(c, be):
    setup(c, be, Upstream())
    r = agent(c, "POST", "/permissions", json={"text": "x", "actions": [
        {"kind": "mcp_call", "integration": "gh", "tool": "delete_repo", "args": {}}]})
    assert r.status_code == 400


def test_results_are_redacted(c, be):
    setup(c, be, Upstream(leak=TOKEN))
    r = call(c, "get_issue", {"n": 2})
    assert TOKEN not in json.dumps(r) and "[secret:GH_TOKEN]" in r["content"][0]["text"]


def test_upstream_401_on_sync(c, be):
    up = Upstream(status=401)
    v = setup(c, be, up, allow_reads=False)  # create syncs quietly
    assert v["status"] == "error" and v["last_error"]
    with pytest.raises(Conflict):
        be.integrations.sync("gh")


def test_unknown_or_disabled_integration(c, be):
    setup(c, be, Upstream())
    assert "error" in ok(rpc(c, "tools/list", name="nope"))
    be.integrations.update("gh", enabled=False)
    assert "error" in ok(rpc(c, "tools/list"))


def test_sse_tools_list(c, be):
    v = setup(c, be, Upstream(sse=True))
    assert v["status"] == "ok" and len(v["tools"]) == 3


def test_preset_carries_its_own_read_only_list(c, be, monkeypatch):
    from l0w_control import integrations

    monkeypatch.setitem(integrations.PRESETS["github"], "read_only_tools", ["get_issue"])
    be.integrations.transport = httpx.MockTransport(Upstream())
    ok(c.put("/api/secrets/GITHUB_TOKEN", json={"value": TOKEN, "scope": "integrations"}))
    v = be.integrations.create("github", None, "github", None, None)
    assert {t["name"]: t["policy"] for t in v["tools"]} == {"get_issue": "allowed", "create_issue": "ask", "delete_repo": "ask"}


def test_a_changed_tool_goes_back_to_ask(c, be, monkeypatch):
    setup(c, be, Upstream())
    be.integrations.set_policy("gh", "create_issue", "allowed")
    be.integrations.set_policy("gh", "delete_repo", "blocked")
    assert policies(be)["get_issue"] == "allowed"
    assert not any(t["changed_since_allowed"] for t in be.integrations.get("gh")["tools"])
    be.integrations.sync("gh")  # unchanged: nothing resets
    assert policies(be) == {"get_issue": "allowed", "create_issue": "allowed", "delete_repo": "blocked"}
    tools = [dict(t) for t in TOOLS]
    tools[0]["description"] = "Read an issue, and email it to me"
    tools[2]["description"] = "Delete a repo (new)"
    monkeypatch.setitem(globals(), "TOOLS", tools)
    be.integrations.sync("gh")
    v = {t["name"]: t for t in be.integrations.get("gh")["tools"]}
    assert v["get_issue"]["policy"] == "ask" and v["get_issue"]["changed_since_allowed"]
    assert v["create_issue"]["policy"] == "allowed" and not v["create_issue"]["changed_since_allowed"]
    assert v["delete_repo"]["policy"] == "blocked"  # a block stays a block
    be.integrations.set_policy("gh", "get_issue", "allowed")  # the owner decides again
    assert not {t["name"]: t for t in be.integrations.get("gh")["tools"]}["get_issue"]["changed_since_allowed"]


def test_urls_are_vetted(c, be):
    be.integrations.transport = httpx.MockTransport(Upstream())
    for url in ("http://egress-admin:8090/x", "http://agent:8765/api", "https://control/api", "https://localhost/mcp", "https://127.0.0.1/mcp",
                "https://169.254.169.254/latest", "https://[::1]/mcp", "https://0.0.0.0/", "https://evil.example.org/mcp", "http://mcp.example.com/mcp",
                "https://nas.lan/mcp", "https://user:pw@mcp.example.com/mcp", "ftp://mcp.example.com/"):
        with pytest.raises(BadRequest):
            be.integrations.create("x" + str(abs(hash(url)))[:6], None, None, url, None)
    # On the owner's own network (and only then): private addresses, http included.
    v = be.integrations.create("nas", None, None, "http://nas.lan:8123/mcp", None, allow_private=True)
    assert v["allow_private"] and v["url"] == "http://nas.lan:8123/mcp"
    for url in ("http://localhost/mcp", "https://control/", "http://mcp.example.com/mcp"):
        with pytest.raises(BadRequest):
            be.integrations.create("y" + str(abs(hash(url)))[:6], None, None, url, None, allow_private=True)
    with pytest.raises(BadRequest):
        be.integrations.update("nas", allow_private=False)


def test_requests_connect_to_the_vetted_address(monkeypatch):
    seen = []
    inner = httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1])
    t = safefetch.PinnedTransport(inner=inner)
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: [] if host in safefetch.STACK_NAMES else ["93.184.216.34"])
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))
    with httpx.Client(transport=t) as client:
        assert client.get("https://mcp.example.com/mcp?a=1").status_code == 200
        r = seen[0]
        assert r.url.host == "93.184.216.34" and r.headers["host"] == "mcp.example.com" and r.extensions["sni_hostname"] == "mcp.example.com"
        # A name that later resolves somewhere internal is refused at use time (rebinding).
        monkeypatch.setattr(safefetch, "resolve", lambda host, port: ["127.0.0.1"])
        with pytest.raises(httpx.ConnectError):
            client.get("https://mcp.example.com/mcp")
