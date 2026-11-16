"""M2 through the real API (no runner): threads, messages, asks, claims, suggestions,
delivery to the agent, secrets and redaction, uploads. The agent's calls use its token."""

import os

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

PW = "correct horse battery"
AGENT = {"X-Agent-Token": "agent-secret"}


@pytest.fixture
def be(tmp_path, monkeypatch):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    return SqliteBackend(env=env)


@pytest.fixture
def c(be):
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        yield c


def agent(c, method, path, **kw):
    return c.request(method, f"/api/agent{path}", headers=AGENT, **kw)


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def test_threads_messages_unread_and_idempotency(c):
    p = ok(c.post("/api/projects", json={"name": "Garden irrigation"}), 201)
    assert p["slug"] == "garden-irrigation"
    t = ok(c.post("/api/threads", json={"title": "Pump test", "project": "garden-irrigation", "first_message": "Can you test the pump?"}), 201)
    assert t["project"] == "garden-irrigation" and t["unread"] == 0
    key = "k-123"
    m1 = ok(c.post(f"/api/threads/{t['id']}/messages", json={"body": "Saturday works.", "idempotency_key": key}), 201)
    m2 = ok(c.post(f"/api/threads/{t['id']}/messages", json={"body": "Saturday works.", "idempotency_key": key}), 201)
    assert m1 == m2 and m1["delivery"] == "queued"
    items = ok(c.get(f"/api/threads/{t['id']}/items"))["items"]
    assert [i["message"]["body"] for i in items] == ["Can you test the pump?", "Saturday works."]

    # The agent replies: unread counts on the thread and the project.
    ok(agent(c, "POST", "/messages", json={"thread": t["id"], "body": "Will do."}))
    assert ok(c.get(f"/api/threads/{t['id']}"))["unread"] == 1
    assert ok(c.get("/api/projects/garden-irrigation"))["unread"] == 1
    ok(c.post(f"/api/threads/{t['id']}/read"))
    assert ok(c.get(f"/api/threads/{t['id']}"))["unread"] == 0
    assert ok(c.get("/api/messages/recent", params={"sender": "assistant"}))[0]["message"]["body"] == "Will do."

    # The agent can start threads and projects of its own.
    ok(agent(c, "POST", "/projects", json={"name": "Home lab", "summary": "servers"}))
    t2 = ok(agent(c, "POST", "/threads", json={"title": "Backups", "project": "home-lab", "body": "Backups failed last night."}))
    assert t2["created_by"] == "assistant" and t2["unread"] == 1
    hits = ok(c.get("/api/search", params={"q": "backups"}))
    assert {h["kind"] for h in hits} >= {"thread", "message"}


def test_delivery_to_the_agent(c):
    t = ok(c.post("/api/threads", json={"title": "Plans", "first_message": "First"}), 201)
    q = ok(agent(c, "POST", "/questions", json={"text": "Which day?", "kind": "single", "options": ["Sat", "Sun"], "default": "Sat", "thread": t["id"]}))
    task = ok(agent(c, "POST", "/tasks", json={"text": "Plug in the pump", "thread": t["id"]}))
    assert ok(c.get("/api/inbox/counts"))["by_kind"] == {"question": 1, "task": 1, "security": 1}
    assert ok(c.get(f"/api/threads/{t['id']}"))["open_asks"] == 2

    assert c.post(f"/api/questions/{q['id']}/answer", json={"value": "Monday"}).status_code == 400
    ok(c.post(f"/api/questions/{q['id']}/answer", json={"value": "Sun", "note": "after lunch"}))
    assert c.post(f"/api/questions/{q['id']}/answer", json={"value": "Sat"}).status_code == 409
    ok(c.post(f"/api/tasks/{task['id']}/resolve", json={"outcome": "done"}))

    peek = ok(agent(c, "GET", "/inbox?peek=true"))
    assert "Owner (message #" in peek["text"] and "Which day?" in peek["text"] and "Sun. Note: after lunch" in peek["text"]
    assert peek["counts"] == {"messages": 1, "answers": 1, "tasks": 1}
    got = ok(agent(c, "GET", "/inbox"))
    assert got["text"] == peek["text"]
    assert ok(agent(c, "GET", "/inbox"))["text"] == ""  # delivered once
    items = ok(c.get(f"/api/threads/{t['id']}/items"))["items"]
    assert items[0]["message"]["delivery"] == "delivered"
    history = ok(c.get("/api/inbox", params={"view": "history"}))
    assert {e["kind"] for e in history} >= {"question", "task"}


def test_the_agent_cannot_decide(c):
    q = ok(agent(c, "POST", "/questions", json={"text": "OK?", "default": True}))
    p = ok(agent(c, "POST", "/permissions", json={"text": "Reach example.org", "actions": [{"kind": "egress_rule", "pattern": "example.org"}]}))
    assert p["actions"][0]["summary"] == "allow read-only (GET over plain HTTP) access to example.org"
    # No agent route answers or decides, and the owner routes refuse the agent token.
    for path in (f"/questions/{q['id']}/answer", f"/permissions/{p['id']}/decide", f"/api/questions/{q['id']}/answer"):
        assert agent(c, "POST", path, json={"value": True, "decision": "allow"}).status_code in (404, 405)
    with TestClient(c.app, client=("10.255.255.9", 1), cookies=c.cookies, headers={"x-l0w-client": "test"}) as sandbox:  # even with the owner's cookie
        r = sandbox.post(f"/api/questions/{q['id']}/answer", json={"value": True}, headers=AGENT)
        assert r.status_code == 403
    # It can only withdraw its own asks.
    ok(agent(c, "POST", f"/questions/{q['id']}/withdraw"))
    assert ok(c.get("/api/inbox", params={"kind": "question"})) == []
    # Bad actions are refused up front; settings must be real.
    assert agent(c, "POST", "/permissions", json={"text": "x", "actions": [{"kind": "setting", "key": "NOPE", "value": 1}]}).status_code == 400
    assert agent(c, "POST", "/permissions", json={"text": "x", "actions": [{"kind": "egress_rule", "pattern": "a b"}]}).status_code == 400
    assert agent(c, "POST", "/questions", json={"text": "no default"}).status_code == 400


def test_permission_actions(c, be):
    t = ok(c.post("/api/threads", json={"title": "Keys", "project": None}), 201)
    p = ok(agent(c, "POST", "/permissions", json={"text": "Use the weather API", "thread": t["id"], "actions": [
        {"kind": "secret", "name": "WEATHER_KEY", "description": "openweathermap"}, {"kind": "setting", "key": "HOLD_MINUTES", "value": "9"}]}))
    assert p["scopes"] == ["once", "always"]
    r = c.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "always"})
    assert r.status_code == 409 and r.json()["error"] == "secret_missing"
    ok(c.put("/api/secrets/WEATHER_KEY", json={"value": "wk-0123456789abcdef"}))
    assert c.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "project"}).status_code == 400
    ok(c.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "always"}))
    assert be.settings.get("HOLD_MINUTES") == 9
    text = ok(agent(c, "GET", "/inbox"))["text"]
    assert "allowed (always)" in text and "New secret WEATHER_KEY" in text
    # A one-time approval is undone when the run ends.
    p2 = ok(agent(c, "POST", "/permissions", json={"text": "Shorter holds", "actions": [{"kind": "setting", "key": "HOLD_MINUTES", "value": "2"}]}))
    ok(c.post(f"/api/permissions/{p2['id']}/decide", json={"decision": "allow", "scope": "once"}))
    assert be.settings.get("HOLD_MINUTES") == 2
    assert be.conv.revert_once(run_id=999, run_started_at="2000-01-01T00:00:00Z") == ["set HOLD_MINUTES to 2"]
    assert be.settings.get("HOLD_MINUTES") == 9


def test_secrets_are_write_only_and_redacted(c, be):
    t = ok(c.post("/api/threads", json={"title": "Oops", "first_message": "my token is ghp_ABCDEFGHIJKLMNOP"}), 201)
    info = ok(c.put("/api/secrets/GITHUB_TOKEN", json={"value": "ghp_ABCDEFGHIJKLMNOP", "description": "repo access"}))
    assert set(info) == {"name", "description", "scope", "created_at", "updated_at", "reads", "last_read_at"}
    assert "ghp_" not in c.get("/api/secrets").text
    # Already-stored text is scrubbed; new text is redacted.
    items = ok(c.get(f"/api/threads/{t['id']}/items"))["items"]
    assert items[0]["message"]["body"] == "my token is [secret:GITHUB_TOKEN]"
    ok(agent(c, "POST", "/messages", json={"thread": t["id"], "body": "I used ghp_ABCDEFGHIJKLMNOP"}))
    assert "ghp_" not in c.get(f"/api/threads/{t['id']}/items").text
    # The agent can read it (logged); people can't.
    assert ok(agent(c, "GET", "/secrets/github_token"))["value"] == "ghp_ABCDEFGHIJKLMNOP"
    assert ok(c.get("/api/secrets"))[0]["reads"] == 1
    assert be.vault.env() == {"GITHUB_TOKEN": "ghp_ABCDEFGHIJKLMNOP"}
    assert c.put("/api/secrets/bad name", json={"value": "x"}).status_code == 400
    ok(c.delete("/api/secrets/GITHUB_TOKEN"))
    assert ok(c.get("/api/secrets")) == []


def test_suggestions_are_carried_out(c):
    ok(c.post("/api/projects", json={"name": "A"}), 201)
    ok(c.post("/api/projects", json={"name": "B"}), 201)
    t1 = ok(c.post("/api/threads", json={"title": "One", "project": "a", "first_message": "m1"}), 201)
    t2 = ok(c.post("/api/threads", json={"title": "Two", "project": "a", "first_message": "m2"}), 201)
    t3 = ok(c.post("/api/threads", json={"title": "Three", "first_message": "m3"}), 201)
    s1 = ok(agent(c, "POST", "/suggestions", json={"kind": "thread_rename", "target": str(t1["id"]), "text": "clearer", "payload": {"title": "Pump firmware"}}))
    s2 = ok(agent(c, "POST", "/suggestions", json={"kind": "thread_merge", "target": str(t2["id"]), "text": "dupe", "payload": {"into": t1["id"]}}))
    ok(agent(c, "POST", "/suggestions", json={"kind": "thread_move", "target": str(t3["id"]), "text": "belongs in B", "payload": {"project": "b"}}))
    ok(agent(c, "POST", "/suggestions", json={"kind": "project_stale", "target": "b", "text": "quiet", "payload": {}}))
    assert agent(c, "POST", "/suggestions", json={"kind": "thread_merge", "target": str(t1["id"]), "text": "x", "payload": {"into": t1["id"]}}).status_code == 400
    ok(c.post(f"/api/suggestions/{s1['id']}/decide", json={"decision": "accept"}))
    assert ok(c.get(f"/api/threads/{t1['id']}"))["title"] == "Pump firmware"
    ok(c.post(f"/api/suggestions/{s2['id']}/decide", json={"decision": "dismiss"}))
    accepted = ok(c.post("/api/suggestions/accept-all"))
    assert {x["kind"] for x in accepted} == {"thread_move", "project_stale"}
    assert ok(c.get(f"/api/threads/{t3['id']}"))["project"] == "b"
    assert ok(c.get("/api/projects/b"))["status"] == "stale"
    # Merge: everything moves, both threads get a notice, the source is archived.
    s3 = ok(agent(c, "POST", "/suggestions", json={"kind": "thread_merge", "target": str(t2["id"]), "text": "dupe", "payload": {"into": t1["id"]}}))
    ok(c.post(f"/api/suggestions/{s3['id']}/decide", json={"decision": "accept"}))
    bodies = [i["message"]["body"] for i in ok(c.get(f"/api/threads/{t1['id']}/items"))["items"] if i["type"] == "message"]
    assert "m2" in bodies and any(b.startswith("Moved here from “Two”") for b in bodies)
    assert ok(c.get(f"/api/threads/{t2['id']}"))["status"] == "archived"
    assert "suggestion" in ok(agent(c, "GET", "/inbox"))["text"]


def test_uploads_are_contained(c, be, tmp_path):
    ok(c.post("/api/projects", json={"name": "Garden"}), 201)
    u = ok(c.post("/api/uploads", files={"file": ("../../etc/notes.txt", b"hello")}, data={"project": "garden"}), 201)
    assert u["path"] == "_inbox/garden/notes.txt"
    assert (tmp_path / "work" / "_inbox" / "garden" / "notes.txt").read_bytes() == b"hello"
    u2 = ok(c.post("/api/uploads", files={"file": ("notes.txt", b"again")}, data={"project": "garden"}), 201)
    assert u2["path"] == "_inbox/garden/notes-2.txt"
    # The agent controls /work: a symlinked folder must not redirect control's write.
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "work" / "_inbox" / "general")
    r = c.post("/api/uploads", files={"file": ("x.txt", b"x")})
    assert r.status_code == 409 and not any(outside.iterdir())
    os.unlink(tmp_path / "work" / "_inbox" / "general")
    (tmp_path / "work" / "_inbox" / "general").mkdir()
    os.symlink(outside / "target", tmp_path / "work" / "_inbox" / "general" / "x.txt")
    u3 = ok(c.post("/api/uploads", files={"file": ("x.txt", b"x")}), 201)
    assert u3["path"] == "_inbox/general/x-2.txt" and not (outside / "target").exists()
    # Attached to a message, the agent sees where it is.
    t = ok(c.post("/api/threads", json={"title": "Photos"}), 201)
    m = ok(c.post(f"/api/threads/{t['id']}/messages", json={"body": "see attached", "upload_ids": [u["id"]]}), 201)
    assert m["attachments"][0]["path"] == "_inbox/garden/notes.txt"
    assert "/work/_inbox/garden/notes.txt" in ok(agent(c, "GET", "/inbox"))["text"]
    # Once delivered, the thread history (a Reply now brief, `assistant messages`) still lists it.
    assert "(attached: /work/_inbox/garden/notes.txt)" in ok(agent(c, "GET", f"/messages?thread={t['id']}"))["text"]


def test_harness_news_announces_the_new_commands(c):
    news = ok(agent(c, "GET", "/news"))
    titles = [n["title"] for n in news]  # newest first
    assert titles[:5] == ["You may run on opencode", "Updates wait for you", "Budgets, your wakeup plan, routines, the reviewer and long jobs",
                          "The shelf and the owner's uploads", "Threads, asks, claims and suggestions"]
    assert "opencode" in news[0]["body"] and "--dont-interrupt" in news[1]["body"] and "assistant wake at" in news[2]["body"]
    assert "assistant shelf add" in news[3]["body"] and "assistant claim N" in news[4]["body"]
