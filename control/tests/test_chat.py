"""M6's 1:1 chat through the real API, with the scripted model (L0W_CHAT_FAKE=1): replies,
tools reading the assistant's data, hand-off, cards (a secret saved without the model
seeing it), usage counted as the owner's own, the providers it can't run on yet, and the
Anthropic and OpenAI-compatible history builders."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from l0w_control import tools as T
from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.chat import AnthropicClient, OpenAICompatClient
from l0w_control.config import Env

PW = "correct horse battery"


@pytest.fixture
def be(tmp_path, monkeypatch):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    monkeypatch.setenv("L0W_CHAT_FAKE", "1")
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    return SqliteBackend(env=env)


@pytest.fixture
def c(be):
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        yield c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def settled(c, chat_id: int, timeout: float = 10) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        if not ok(c.get(f"/api/chats/{chat_id}"))["busy"]:
            return ok(c.get(f"/api/chats/{chat_id}/messages"))
        time.sleep(0.05)
    raise AssertionError("the chat didn't finish replying")


def test_a_chat_replies_and_counts_as_the_owners_use(c):
    chat = ok(c.post("/api/chats", json={"message": "Hello there", "idempotency_key": "k-chat-1"}), 201)
    assert chat["title"] == "Hello there" and chat["tier"] == "sonnet/medium"
    msgs = settled(c, chat["id"])
    assert [m["role"] for m in msgs] == ["owner", "assistant"] and msgs[1]["body"] == "You said: Hello there"
    again = ok(c.post("/api/chats", json={"message": "Hello there", "idempotency_key": "k-chat-1"}), 201)
    assert again["id"] == chat["id"]
    budget = ok(c.get("/api/budget"))["providers"][0]
    assert budget["used_by_chat"] > 0 and budget["used_by_assistant"] == 0
    assert [x["id"] for x in ok(c.get("/api/chats"))] == [chat["id"]]


def test_tools_read_the_assistant_and_hand_work_over(c):
    t = ok(c.post("/api/threads", json={"title": "Pump test", "first_message": "Run the pump test"}), 201)
    chat = ok(c.post("/api/chats", json={"message": "fake:tools what's going on?"}), 201)
    msgs = settled(c, chat["id"])
    tools = [m for m in msgs if m["role"] == "tool"]
    assert [x["data"]["name"] for x in tools] == ["list_threads", "status"]
    assert "Pump test" in tools[0]["data"]["result"] and not tools[0]["data"]["is_error"]
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["body"].startswith("Done")
    # The model starts a thread for the assistant, as the owner, labeled via chat.
    ok(c.post(f"/api/chats/{chat['id']}/messages", json={"body": "fake:handoff please water the tomatoes"}), 201)
    msgs = settled(c, chat["id"])
    made = json.loads(next(m for m in msgs if m["role"] == "tool" and m["data"]["name"] == "create_thread")["data"]["result"])
    items = ok(c.get(f"/api/threads/{made['created']['thread_id']}/items"))["items"]
    assert items[0]["message"]["via"] == f"chat:{chat['id']}"
    # Move to the assistant: a thread with the summary and the transcript attached.
    moved = ok(c.post(f"/api/chats/{chat['id']}/handoff", json={"title": "From my chat"}))
    assert moved["handed_off_to"]["title"] == "From my chat"
    first = ok(c.get(f"/api/threads/{moved['handed_off_to']['id']}/items"))["items"][0]["message"]
    assert "Handed over from a 1:1 chat" in first["body"] and first["attachments"][0]["name"] == f"chat-{chat['id']}.md"
    # Ask the assistant in an existing thread; pull a thread in as context.
    ok(c.post(f"/api/chats/{chat['id']}/ask", json={"thread_id": t["id"], "message": "Any news on the pump?"}))
    assert ok(c.get(f"/api/threads/{t['id']}/items"))["items"][-1]["message"]["body"] == "Any news on the pump?"
    assert ok(c.post(f"/api/chats/{chat['id']}/context", json={"thread_id": t["id"]}))["context_threads"] == [t["id"]]


def test_a_secret_card_saves_without_the_model_seeing_it(c):
    chat = ok(c.post("/api/chats", json={"message": "fake:secret set up github"}), 201)
    msgs = settled(c, chat["id"])
    card = next(m for m in msgs if m["role"] == "card")
    assert card["data"]["kind"] == "secret" and card["data"]["name"] == "GITHUB_TOKEN" and card["data"]["state"] == "open"
    token = "ghp_secretvalue0123456789abcdef"
    assert c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve"}).status_code == 400  # no value
    done = ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve", "value": token}))
    assert done["data"]["state"] == "done"
    assert [x["name"] for x in ok(c.get("/api/secrets"))] == ["GITHUB_TOKEN"]
    everything = json.dumps(ok(c.get(f"/api/chats/{chat['id']}/messages")))
    assert token not in everything and "saved the secret GITHUB_TOKEN" in everything
    assert c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve", "value": token}).status_code == 409
    # A secret pasted into the chat by mistake is redacted from what's stored.
    ok(c.post(f"/api/chats/{chat['id']}/messages", json={"body": f"here it is {token}"}), 201)
    assert token not in json.dumps(settled(c, chat["id"]))


def test_errors_end_the_turn_and_the_subscription_needs_the_sidecar(c, be, monkeypatch):
    chat = ok(c.post("/api/chats", json={"message": "fake:fail"}), 201)
    msgs = settled(c, chat["id"])
    assert msgs[-1]["role"] == "system" and "Fake failure" in msgs[-1]["body"]
    be.chats_.fake = False
    other = ok(c.post("/api/chats", json={"message": "hi"}), 201)
    assert not other["available"]
    assert "chat runtime" in settled(c, other["id"])[-1]["body"]


def test_tools_check_their_input():
    ctx = T.Ctx(be=None)
    assert T.run(ctx, "nope", {}) == ("No tool named nope.", True)
    assert T.run(ctx, "read_thread", {"thread_id": 1, "evil": 1})[1]
    assert "Secret names" in T.run(ctx, "ask_for_secret", {"name": "bad name!"})[0]
    assert T.run(ctx, "propose_level", {"level": "huge"})[1]
    msg, err = T.run(ctx, "propose_egress_rule", {"host": "api.github.com"})
    assert not err and ctx.cards == [{"kind": "egress", "host": "api.github.com", "action": "allow", "reason": None, "state": "open"}]
    assert T.summary(["read_thread", "read_thread", "search"]) == "Read 2 threads · searched"


ROWS = [
    {"role": "owner", "body": "hi", "data": {}, "raw_json": None, "provider": None},
    {"role": "assistant", "body": "Let me look.", "data": {"calls": [{"id": "c1", "name": "status", "input": {}}]}, "raw_json": None, "provider": "opencode-go"},
    {"role": "tool", "body": "", "data": {"call_id": "c1", "name": "status", "result": "{}", "is_error": False}, "raw_json": None, "provider": None},
    {"role": "system", "body": "The owner saved the secret X.", "data": {}, "raw_json": None, "provider": None},
]


def test_histories_for_both_apis():
    a = AnthropicClient.history(ROWS)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]
    assert a[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "status", "input": {}}
    assert a[2]["content"][0]["type"] == "tool_result" and a[2]["content"][1]["text"].startswith("[control]")
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "Hi"}]
    replay = AnthropicClient.history([ROWS[0], {**ROWS[1], "data": {"calls": []}, "raw_json": json.dumps(raw), "provider": "anthropic-api"}])
    assert replay[1]["content"] == raw   # replayed unchanged, thinking included
    o = OpenAICompatClient.history("SYS", ROWS)
    assert [m["role"] for m in o] == ["system", "user", "assistant", "tool", "user"]
    assert o[2]["tool_calls"][0]["function"] == {"name": "status", "arguments": "{}"} and o[3]["tool_call_id"] == "c1"


def test_the_chat_proposes_settings_and_the_owner_approves(c):
    chat = ok(c.post("/api/chats", json={"message": "fake:settings it's a deadline week"}), 201)
    card = next(m for m in settled(c, chat["id"]) if m["role"] == "card")
    assert card["data"]["kind"] == "settings"
    assert [(x["label"].split(" (")[0], x["before"], x["after"]) for x in card["data"]["changes"]] == [("level", "steady", "busy"), ("reply pts", "30.0", "50")]
    assert ok(c.get("/api/budget"))["providers"][0]["level"] == "steady"   # nothing applied yet
    ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve"}))
    b = ok(c.get("/api/budget"))["providers"][0]
    assert b["level"] == "busy" and b["reply_allowance"] == 50
    assert "approved the settings changes: level" in json.dumps(ok(c.get(f"/api/chats/{chat['id']}/messages")))


def test_the_chat_proposes_a_handoff(c):
    chat = ok(c.post("/api/chats", json={"message": "fake:propose-handoff water the tomatoes"}), 201)
    card = next(m for m in settled(c, chat["id"]) if m["role"] == "card")
    assert card["data"]["kind"] == "handoff" and card["data"]["mode"] == "transcript"
    done = ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve"}))
    tid = done["data"]["thread_id"]
    first = ok(c.get(f"/api/threads/{tid}/items"))["items"][0]["message"]
    assert first["body"].startswith("Please set up the drip timer") and first["attachments"][0]["name"] == f"chat-{chat['id']}.md"
    assert ok(c.get(f"/api/chats/{chat['id']}"))["handed_off_to"]["id"] == tid


def test_tools_check_settings_and_read_the_guide(be):
    from l0w_control import tools as T
    ctx = T.Ctx(be=be)
    topics, err = T.run(ctx, "help", {})
    assert not err and "budgets" in topics and "providers-models" in topics
    assert "1 pt is 0.1%" in T.run(ctx, "help", {"topic": "budgets"})[0]
    assert "[budgets]" in T.run(ctx, "help", {"query": "reserve weekly"})[0]
    assert "Unknown setting" in T.run(ctx, "propose_settings", {"changes": [{"setting": "password", "value": "x"}]})[0]
    assert "Levels are" in T.run(ctx, "propose_settings", {"changes": [{"setting": "level", "value": "huge"}]})[0]
    assert T.run(ctx, "propose_handoff", {"target": "elsewhere", "mode": "message", "message": "x"})[1]


def test_a_chat_may_use_any_model_its_provider_offers(be):
    be.budgets.add_api_provider("sk-go-test-0123456789abcdefghij", None, "opencode-go")
    spec = be.budgets.runtime_spec("opencode-go", "opencode-go/kimi-k3/max")  # in Go's catalog, not enabled for the assistant
    assert spec == {"runtime": "opencode", "model": "opencode-go/kimi-k3", "effort": None}  # no known variants: the model decides
    assert be.budgets.runtime_spec("opencode-go", "opencode-go/glm-5.3/high")["effort"] == "high"


def test_files_in_a_chat_reach_the_model_and_go_along_on_a_handoff(c):
    up = ok(c.post("/api/chat-files", files={"file": ("notes.txt", b"pump: 2 bar", "text/plain")}), 201)
    img = ok(c.post("/api/chat-files", files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")}), 201)
    chat = ok(c.post("/api/chats", json={"message": "what do you think?", "upload_ids": [up["id"], img["id"]]}), 201)
    msgs = settled(c, chat["id"])
    assert [f["name"] for f in msgs[0]["data"]["files"]] == ["notes.txt", "photo.png"]
    assert msgs[1]["body"] == "You said: what do you think? (attached: notes.txt, photo.png)"
    assert c.get(f"/api/chat-files/{up['id']}").content == b"pump: 2 bar"
    assert c.post(f"/api/chats/{chat['id']}/messages", json={"body": "again", "upload_ids": [up["id"]]}).status_code == 400  # already sent
    # On a hand-off the chat's files go to the assistant too.
    ok(c.post(f"/api/chats/{chat['id']}/messages", json={"body": "fake:propose-handoff"}), 201)
    card = next(m for m in settled(c, chat["id"]) if m["role"] == "card")
    done = ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve"}))
    first = ok(c.get(f"/api/threads/{done['data']['thread_id']}/items"))["items"][0]["message"]
    assert {a["name"] for a in first["attachments"]} >= {"notes.txt", "photo.png"}


def test_history_builders_send_files_to_each_api():
    f_txt = {"name": "a.txt", "mime": "text/plain", "size": 3, "data": b"abc"}
    f_png = {"name": "p.png", "mime": "image/png", "size": 4, "data": b"\x89PNG"}
    f_pdf = {"name": "d.pdf", "mime": "application/pdf", "size": 4, "data": b"%PDF"}
    row = {"role": "owner", "body": "look", "data": {}, "raw_json": None, "provider": None, "blobs": [f_txt, f_png, f_pdf]}
    a = AnthropicClient.history([row])[0]["content"]
    assert [b["type"] for b in a] == ["text", "image", "document", "text"] and "abc" in a[0]["text"]
    o = OpenAICompatClient.history("S", [row])[1]["content"]
    assert [p["type"] for p in o] == ["text", "image_url", "text", "text"] and "can't read" in o[2]["text"]
