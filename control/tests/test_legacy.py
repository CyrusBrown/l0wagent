"""M7: importing the old assistant's export (legacy.py)."""

import json

from l0w_control import legacy
from tests.test_conversations import be, c, ok  # noqa: F401

EXPORT = {
    "projects": [{"id": 1, "slug": "homeserver", "name": "Home server"}],
    "messages": [
        {"id": 1, "created_at": "2026-10-01T10:00:00+00:00", "sender": "user", "body": "hi, my key is sk-old-0123456789", "project_id": None, "run_id": None},
        {"id": 2, "created_at": "2026-10-01T10:05:00+00:00", "sender": "assistant", "body": "hello", "project_id": None, "run_id": 7},
        {"id": 3, "created_at": "2026-10-02T09:00:00+00:00", "sender": "user", "body": "fix jellyfin", "project_id": 1, "run_id": None},
    ],
    "questions": [{"created_at": "2026-10-02T09:30:00+00:00", "project_id": 1, "title": "Which disk?", "details": "", "kind": "single",
                   "options": json.dumps(["a", "b"]), "status": "answered", "answer": json.dumps("b"), "note": None, "run_id": 8}],
    "permissions": [{"created_at": "2026-10-02T09:40:00+00:00", "project_id": 1, "title": "LAN access", "details": "jellyfin",
                     "status": "approved", "response": "ok", "run_id": 8}],
    "tasks": [],
    "egress_rules": [{"pattern": "10.0.0.12:8096", "action": "allow_ro", "note": "jellyfin"}, {"pattern": "x", "action": "weird"}],
}


def test_secrets_then_threads(c, be):  # noqa: F811
    out = legacy.import_secrets(be.db, be.vault, {"secrets": [{"name": "OLD_KEY", "value": "sk-old-0123456789", "description": ""},
                                                             {"name": "bad name", "value": "x"}]})
    assert out == {"imported": ["OLD_KEY"], "skipped": ["bad name"]}
    assert be.vault.env(None)["OLD_KEY"] == "sk-old-0123456789"

    summary = legacy.import_legacy(be.db, be.vault, EXPORT)
    titles = [t["title"] for t in summary["threads"]]
    assert titles == ["General (old assistant)", "Home server (old assistant)"]
    general = summary["threads"][0]["thread_id"]
    t = ok(c.get(f"/api/threads/{general}"))
    assert t["status"] == "archived"
    items = ok(c.get(f"/api/threads/{general}/items"))["items"]
    bodies = [i["message"]["body"] for i in items]
    assert "sk-old" not in json.dumps(bodies) and "[secret:OLD_KEY]" in bodies[0] and "old run #7" in bodies[1]
    assert items[0]["at"].startswith("2026-10-01T10:00")
    home = ok(c.get(f"/api/threads/{summary['threads'][1]['thread_id']}/items"))["items"]
    text = json.dumps([i["message"]["body"] for i in home])
    assert "Old question" in text and "Answered: b" in text and "Old permission" in text and "approved: ok" in text
    resetup = ok(c.get(f"/api/threads/{summary['resetup_thread_id']}"))
    assert resetup["status"] == "open" and resetup["title"] == legacy.RESETUP_TITLE
    first = ok(c.get(f"/api/threads/{summary['resetup_thread_id']}/items"))["items"][0]["message"]
    assert "/work/_legacy/" in first["body"] and "<!--" not in first["body"]
    # Once only.
    again = legacy.import_legacy(be.db, be.vault, EXPORT)
    assert again["skipped"] == "already imported"
    assert be.db.q1("SELECT COUNT(*) AS n FROM threads WHERE status='archived'")["n"] == 2


def test_egress_rules(be):  # noqa: F811
    added = []

    async def add_rule(pattern, action, note):
        added.append((pattern, action, note))
        return 1

    out = legacy.import_egress(be.db, EXPORT["egress_rules"], add_rule)
    assert out["imported"] == ["10.0.0.12:8096"] and out["failed"][0]["pattern"] == "x"
    assert added[0][2] == "jellyfin (from the old assistant)"
    assert legacy.import_egress(be.db, EXPORT["egress_rules"], add_rule) == {"skipped": "already imported"}
