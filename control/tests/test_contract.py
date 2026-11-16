import inspect
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from l0w_control import db
from l0w_control.app import create_app
from l0w_control.backends.base import Backend
from l0w_control.mock.backend import MockBackend


@pytest.fixture
def client():
    return TestClient(create_app(MockBackend()))


# ── schema ─────────────────────────────────────────────────────────────────
def test_migrations_apply_and_are_idempotent(tmp_path):
    conn = db.connect(tmp_path / "control.db")
    assert db.migrate(conn) == len(db.migrations())
    assert db.migrate(conn) == len(db.migrations())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "auth_sessions", "projects", "threads", "messages", "questions", "runs", "run_sessions", "shelf_items", "change_log"} <= tables


def test_schema_enforces_single_owner(tmp_path):
    conn = db.connect(tmp_path / "control.db")
    db.migrate(conn)
    ins = "INSERT INTO users (id, username, password_hash, created_at, password_changed_at) VALUES (?, ?, 'x', 'now', 'now')"
    conn.execute(ins, (1, "mara"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (2, "someone"))


# ── contract ───────────────────────────────────────────────────────────────
def test_openapi_json_is_current():
    from scripts.export_openapi import OUT, render

    assert OUT.read_text() == render(), "run: uv run python scripts/export_openapi.py"


LIFECYCLE = {"startup", "shutdown"}  # optional hooks with no-op defaults


def test_mock_implements_the_whole_backend():
    missing = [name for name, fn in inspect.getmembers(Backend, inspect.isfunction)
               if not name.startswith("_") and name not in LIFECYCLE and getattr(MockBackend, name) is fn]
    assert missing == []


def test_unimplemented_backend_returns_501():
    c = TestClient(create_app(Backend()))
    r = c.get("/api/meta")
    assert r.status_code == 501 and r.json()["error"] == "not_implemented"


def test_reserved_hub_prefixes_are_unused(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith(("/api/peer", "/api/hub", "/api/agent"))]


# ── auth ───────────────────────────────────────────────────────────────────
def test_login_required_when_enabled():
    c = TestClient(create_app(MockBackend(require_login=True)))
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/auth/login", json={"username": "mara", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "mara", "password": "password"})
    assert r.json()["status"] == "ok" and "l0w_session" in r.cookies
    assert c.get("/api/status").status_code == 200


def test_totp_and_device_token():
    c = TestClient(create_app(MockBackend(require_login=True)))
    r = c.post("/api/auth/login", json={"username": "totp-mara", "password": "password", "want": "device_token"}).json()
    assert r["status"] == "totp_required"
    assert c.post("/api/auth/totp", json={"challenge": r["challenge"], "code": "000000"}).status_code == 401
    token = c.post("/api/auth/totp", json={"challenge": r["challenge"], "code": "123456"}).json()["device_token"]
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["username"] == "l0w"


# ── conversations ──────────────────────────────────────────────────────────
def test_post_message_is_idempotent(client):
    body = {"body": "hello", "idempotency_key": "k1"}
    a = client.post("/api/threads/1/messages", json=body).json()
    b = client.post("/api/threads/1/messages", json=body).json()
    assert a["id"] == b["id"] and a["delivery"] == "queued"
    items = client.get("/api/threads/1/items").json()["items"]
    assert sum(1 for i in items if i["type"] == "message" and i["message"]["body"] == "hello") == 1


def test_thread_items_interleave_cards(client):
    kinds = {i["type"] for i in client.get("/api/threads/1/items").json()["items"]}
    assert {"message", "question", "permission", "claim", "suggestion", "promote"} <= kinds


def test_answering_updates_inbox(client):
    before = client.get("/api/inbox/counts").json()["waiting"]
    q = client.post("/api/questions/2/answer", json={"value": "Move to 05:30, outside the DST hour", "note": "fine"}).json()
    assert q["status"] == "answered"
    assert client.get("/api/inbox/counts").json()["waiting"] == before - 1
    assert client.post("/api/questions/2/answer", json={"value": "Skip Sundays entirely"}).status_code == 409
    assert any(e["id"] == 2 for e in client.get("/api/inbox", params={"view": "history", "kind": "question"}).json())


def test_invalid_answer_rejected(client):
    assert client.post("/api/questions/2/answer", json={"value": "not an option"}).status_code == 409
    assert client.post("/api/questions/1/answer", json={"value": "yes"}).status_code == 409


def test_permission_scope_must_be_offered(client):
    assert client.post("/api/permissions/1/decide", json={"decision": "allow", "scope": "always"}).status_code == 409
    assert client.post("/api/permissions/1/decide", json={"decision": "allow", "scope": "project"}).json()["status"] == "allowed"


def test_accepting_archive_suggestion_archives_thread(client):
    client.post("/api/suggestions/2/decide", json={"decision": "accept"})
    assert client.get("/api/threads/5").json()["status"] == "archived"


def test_sync_feed(client):
    snap = client.get("/api/sync").json()
    client.post("/api/threads", json={"title": "New idea", "project": "home-lab"})
    changes = client.get("/api/sync", params={"since": snap["cursor"]}).json()["changes"]
    assert [c["entity"] for c in changes] == ["thread"]
    assert client.get("/api/sync", params={"since": "garbage"}).json()["reset"] is True


# ── runs ───────────────────────────────────────────────────────────────────
def test_simple_timeline_is_a_subset(client):
    simple = client.get("/api/runs/123/timeline").json()
    detailed = client.get("/api/runs/123/timeline", params={"view": "detailed"}).json()
    assert 0 < len(simple) < len(detailed)
    assert all(i["simple"] for i in simple)


def test_stop_run(client):
    assert client.post("/api/runs/123/stop").json()["status"] == "stopped"
    assert client.get("/api/status").json()["state"] == "asleep"


# ── files ──────────────────────────────────────────────────────────────────
def test_listing_and_traversal(client):
    ls = client.get("/api/files/list", params={"volume": "workspace", "path": ""}).json()
    assert ls["git"] and {"NOW.md", "projects", "journal"} <= {e["name"] for e in ls["entries"]}
    assert client.get("/api/files/list", params={"volume": "workspace", "path": "../work"}).status_code == 404


def test_agent_html_is_never_served_as_html(client):
    r = client.get("/api/files/raw", params={"volume": "work", "path": "garden-irrigation/firmware/out/dashboard.html"})
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-security-policy"] == "sandbox"


def test_upload_lands_in_project_inbox(client):
    r = client.post("/api/uploads", data={"project": "garden-irrigation"}, files={"file": ("../evil.txt", b"hi", "text/plain")}).json()
    assert r["path"] == "garden-irrigation/_inbox/evil.txt"
    names = [e["name"] for e in client.get("/api/files/list", params={"volume": "work", "path": "garden-irrigation/_inbox"}).json()["entries"]]
    assert "evil.txt" in names


# ── live stream ────────────────────────────────────────────────────────────
def test_event_stream_starts_with_hello():
    import asyncio

    backend = MockBackend()

    async def first():
        gen = backend.events()
        event = await gen.__anext__()
        await gen.aclose()
        return event

    event = asyncio.run(first())
    assert event.type == "hello" and json.dumps(event.model_dump(mode="json"))
