"""Claude-subscription chats through the chat-runtime sidecar, control side: the sidecar is a
fake (an httpx mock transport) that behaves like Claude Code, calling control's MCP tools with
the per-turn token and answering in stream-json."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.chat import SidecarClient
from l0w_control.config import Env

PW = "correct horse battery"
RT_TOKEN = "rt-secret"
HEADERS = {"x-l0w-client": "test"}


def make_backend(tmp_path, monkeypatch, *, chat_subnet="", url="http://sidecar:9000", token=RT_TOKEN, expires_ms=None):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    monkeypatch.delenv("L0W_CHAT_FAKE", raising=False)
    home = tmp_path / "agent-home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    exp = expires_ms if expires_ms is not None else int((time.time() + 3600) * 1000)
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "oauth-abc", "expiresAt": exp}}))
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=home, chat_runtime_url=url, chat_runtime_token=token, chat_subnet=chat_subnet,
              chat_control_url="http://control:8000")
    return SqliteBackend(env=env)


def login(c):
    c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
    c.post("/api/auth/login", json={"username": "mara", "password": PW})


@pytest.fixture
def be(tmp_path, monkeypatch):
    return make_backend(tmp_path, monkeypatch)


@pytest.fixture
def c(be):
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers=HEADERS) as c:
        login(c)
        yield c


@pytest.fixture(autouse=True)
def restore_transport():
    old = SidecarClient.transport
    yield
    SidecarClient.transport = old


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def settled(c, chat_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not ok(c.get(f"/api/chats/{chat_id}"))["busy"]:
            return ok(c.get(f"/api/chats/{chat_id}/messages"))
        time.sleep(0.05)
    raise AssertionError("the chat didn't finish replying")


def ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def result_events(text="Here you go"):
    return [
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Here you "}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "result": text, "total_cost_usd": 0.01,
         "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3}},
        {"type": "runtime", "event": "exit", "returncode": 0, "stderr": ""},
    ]


def install_sidecar(be, tool_calls=(), events=None, status=200, body=None):
    """The fake sidecar. Returns the list of requests it saw and the MCP replies it got."""
    seen: list[dict] = []
    replies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/turn"
        assert request.headers["authorization"] == f"Bearer {RT_TOKEN}"
        req = json.loads(request.content)
        seen.append(req)
        if status != 200:
            return httpx.Response(status, content=body or b"nope")
        chats = be.chats_
        chat_id = chats.turn_chat("Bearer " + req["mcp_token"])
        assert chat_id is not None
        seq = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        seq += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(tool_calls)]
        for m in seq:
            replies.append(await asyncio.to_thread(chats.mcp, chat_id, m))
        return httpx.Response(200, content=ndjson(*(events if events is not None else result_events())))

    SidecarClient.transport = httpx.MockTransport(handler)
    return seen, replies


def test_available_needs_the_runtime(be, tmp_path, monkeypatch):
    provider = next(iter(be.chats_.budgets.provider_ids()))
    assert be.chats_.available(provider) is None
    be2 = make_backend(tmp_path / "b", monkeypatch, url="", token="")
    assert "chat runtime" in be2.chats_.available(provider)
    be3 = make_backend(tmp_path / "c", monkeypatch, url="http://x", token="")
    assert "chat runtime" in be3.chats_.available(provider)


def test_a_turn_through_the_sidecar(c, be):
    seen, replies = install_sidecar(be, tool_calls=[("list_projects", {})])
    chat = ok(c.post("/api/chats", json={"message": "What projects exist?"}), 201)
    msgs = settled(c, chat["id"])
    assert len(seen) == 1
    req = seen[0]
    assert req["oauth_token"] == "oauth-abc"
    spec = be.chats_.budgets.runtime_spec(chat["provider"], chat["tier"])
    assert req["model"] == spec["model"] and req["effort"] == spec["effort"]
    assert "What projects exist?" in req["prompt"] and req["system"]
    assert req["mcp_url"] == "http://control:8000/api/chat-runtime/mcp"
    assert replies[0]["result"]["serverInfo"]["name"] == "l0w"
    assert any(t["name"] == "list_projects" for t in replies[1]["result"]["tools"])
    assert replies[2]["result"]["isError"] is False
    roles = [m["role"] for m in msgs]
    tool_i = next(i for i, m in enumerate(msgs) if m["role"] == "tool" and m["data"]["name"] == "list_projects")
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["body"] == "Here you go"
    assert tool_i < len(msgs) - 1 and roles.count("assistant") == 1
    rows = be.db.q("SELECT * FROM usage_ledger WHERE who='chat'")
    assert len(rows) == 1 and rows[0]["chat_id"] == chat["id"] and rows[0]["cost_usd"] == pytest.approx(0.01)
    assert rows[0]["input_tokens"] == 10 and rows[0]["output_tokens"] == 5


def test_a_card_tool_adds_a_card(c, be):
    install_sidecar(be, tool_calls=[("propose_wakeup", {"budget": 5, "reason": "x"})])
    chat = ok(c.post("/api/chats", json={"message": "wake me"}), 201)
    msgs = settled(c, chat["id"])
    card = next(m for m in msgs if m["role"] == "card")
    assert card["data"]["kind"] == "wakeup" and card["data"]["budget"] == 5 and card["data"]["state"] == "open"
    assert msgs[-1]["body"] == "Here you go"


def test_the_mcp_token_lives_only_for_the_turn(c, be):
    seen, _ = install_sidecar(be)
    chat = ok(c.post("/api/chats", json={"message": "hi"}), 201)
    settled(c, chat["id"])
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = c.post("/api/chat-runtime/mcp", json=rpc, headers={"Authorization": "Bearer " + seen[0]["mcp_token"]})
    assert r.status_code == 401
    assert c.post("/api/chat-runtime/mcp", json=rpc, headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert c.post("/api/chat-runtime/mcp", json=rpc).status_code == 401
    # A live token works over HTTP, and is bound to its chat.
    tok = be.chats_.issue_turn_token(chat["id"])
    h = {"Authorization": "Bearer " + tok}
    assert ok(c.post("/api/chat-runtime/mcp", json=rpc, headers=h)) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}
    assert ok(c.post("/api/chat-runtime/mcp", json=call, headers=h))["result"]["isError"] is False
    assert c.post("/api/chat-runtime/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h).status_code == 202
    be.chats_.revoke_turn_token(tok)
    assert c.post("/api/chat-runtime/mcp", json=rpc, headers=h).status_code == 401


def test_expired_credentials_end_the_turn_without_calling_the_sidecar(tmp_path, monkeypatch):
    be = make_backend(tmp_path, monkeypatch, expires_ms=int((time.time() - 3600) * 1000))
    seen, _ = install_sidecar(be)
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers=HEADERS) as c:
        login(c)
        chat = ok(c.post("/api/chats", json={"message": "hi"}), 201)
        msgs = settled(c, chat["id"])
    assert msgs[-1]["role"] == "system" and "expired" in msgs[-1]["body"]
    assert seen == []


def test_sidecar_failures_end_the_turn_with_a_message(c, be):
    install_sidecar(be, status=500, body=b"boom")
    chat = ok(c.post("/api/chats", json={"message": "hi"}), 201)
    msgs = settled(c, chat["id"])
    assert msgs[-1]["role"] == "system" and "HTTP 500" in msgs[-1]["body"] and "boom" in msgs[-1]["body"]
    assert not ok(c.get(f"/api/chats/{chat['id']}"))["busy"]
    assert be.chats_.turn_tokens == {}
    # No result line: Claude Code died.
    install_sidecar(be, events=[{"type": "runtime", "event": "exit", "returncode": 1, "stderr": "segfault"}])
    ok(c.post(f"/api/chats/{chat['id']}/messages", json={"body": "again"}), 201)
    msgs = settled(c, chat["id"])
    assert msgs[-1]["role"] == "system" and "without a reply" in msgs[-1]["body"] and "segfault" in msgs[-1]["body"]
    assert not ok(c.get(f"/api/chats/{chat['id']}"))["busy"]
    assert be.chats_.turn_tokens == {}


def test_unreachable_sidecar(c, be):
    def boom(request):
        raise httpx.ConnectError("down")

    SidecarClient.transport = httpx.MockTransport(boom)
    chat = ok(c.post("/api/chats", json={"message": "hi"}), 201)
    msgs = settled(c, chat["id"])
    assert msgs[-1]["role"] == "system" and "reach the chat runtime" in msgs[-1]["body"]


def test_the_guard_limits_who_reaches_the_mcp_endpoint(tmp_path, monkeypatch):
    be = make_backend(tmp_path, monkeypatch, chat_subnet="10.99.0.0/24")
    app = create_app(be)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with TestClient(app, client=("192.168.1.20", 5000), headers=HEADERS) as owner:
        r = owner.post("/api/chat-runtime/mcp", json=rpc)
        assert r.status_code == 403 and r.json()["error"] == "chat_runtime_only"
    with TestClient(app, client=("10.99.0.5", 1234), headers=HEADERS) as sidecar:
        assert sidecar.post("/api/chat-runtime/mcp", json=rpc).status_code == 401  # reaches the route; no token
        tok = be.chats_.issue_turn_token(1)
        h = {"Authorization": "Bearer " + tok}
        assert sidecar.post("/api/chat-runtime/mcp", json=rpc, headers=h).status_code == 200
        r = sidecar.get("/api/chats", headers=h)
        assert r.status_code == 403 and r.json()["error"] == "forbidden_from_chat_runtime"
        assert sidecar.get("/api/health").status_code == 403


def test_the_transcript_puts_the_latest_owner_message_last():
    rows = [
        {"role": "owner", "body": "first question", "blobs": []},
        {"role": "assistant", "body": "first answer", "data": {}},
        {"role": "tool", "body": "", "data": {"name": "status", "result": "all quiet"}},
        {"role": "system", "body": "Stopped."},
        {"role": "owner", "body": "second question", "blobs": [{"name": "a.png"}]},
    ]
    t = SidecarClient.transcript(rows)
    assert t.startswith("The conversation so far:")
    earlier, _, latest = t.partition("Reply to the owner's latest message:")
    assert "Owner: first question" in earlier and "You: first answer" in earlier and "[tool status" in earlier and "[note: Stopped.]" in earlier
    assert "second question" not in earlier
    assert latest.strip().startswith("second question") and "(attached: a.png" in latest
    only = SidecarClient.transcript([{"role": "owner", "body": "hi", "blobs": []}])
    assert "The conversation so far" not in only and only.endswith("hi")
