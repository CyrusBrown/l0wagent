# ruff: noqa: F811
"""Brokered MCP integrations: sync, the agent's MCP endpoint, the ask flow, redaction. Upstream is faked with httpx.MockTransport."""

import json

import httpx
import pytest

from l0w_control.backends.base import Conflict

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


def setup(c, be, up, secret=TOKEN, name="gh"):
    be.integrations.transport = httpx.MockTransport(up)
    if secret:
        ok(c.put("/api/secrets/GH_TOKEN", json={"value": secret, "scope": "integrations"}))
    return be.integrations.create(name, "GitHub", None, "https://mcp.example.com/mcp", "GH_TOKEN")


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
    v = setup(c, be, up)
    assert v["status"] == "ok" and v["secret_saved"]
    assert policies(be) == {"get_issue": "allowed", "create_issue": "ask", "delete_repo": "ask"}
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
    v = setup(c, be, up)  # create syncs quietly
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
