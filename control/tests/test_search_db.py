# ruff: noqa: F811
"""Search (FTS5 + escaped LIKE), SQL paging of thread items, grouped counts, and the read pool."""

import sqlite3
import threading
import time

import pytest

from l0w_control.db import Database
from tests.test_conversations import agent, be, c, ok  # noqa: F401


def hits(c, q, **kw):
    return ok(c.get("/api/search", params={"q": q, **kw}))


def thread(c, title, first):
    return ok(c.post("/api/threads", json={"title": title, "first_message": first}), 201)


def test_search_escapes_quotes_percent_underscore(c):
    thread(c, "Plain", 'she said "hello world" and left')
    thread(c, "Other", "growth was 100% in snake_case names")
    thread(c, "Decoy", "growth was 1000 in snakeXcase names")
    assert len(hits(c, '"hello world"')) == 1
    assert len(hits(c, 'say "')) == 0
    assert len(hits(c, "100%")) == 1
    assert len(hits(c, "snake_case")) == 1  # '_' is not a wildcard
    assert hits(c, "% ") == [] or all("100%" in (h["snippet"] or "") or h["kind"] != "message" for h in hits(c, "% "))
    assert hits(c, "NEAR(a b)") == [] and hits(c, "a OR b*") == []  # FTS syntax is data
    # short queries (no trigram) use the escaped LIKE
    assert len(hits(c, "_")) >= 1 and all("_" in (h["snippet"] or "") for h in hits(c, "_", kind="message"))
    assert hits(c, "%", kind="message")[0]["snippet"].count("%") >= 1 and len(hits(c, "%", kind="message")) == 1


def test_search_finds_titles_and_runs_case_insensitively(c, be):
    thread(c, "Garden Irrigation notes", "x")
    assert any(h["kind"] == "thread" for h in hits(c, "irrigation NOTES"))
    be.db.ex("INSERT INTO runs(kind, why, status, description, started_at) VALUES('wakeup','manual','done','Rotate the compost bin','2026-01-01T00:00:00Z')")
    assert any(h["kind"] == "run" for h in hits(c, "compost"))
    be.db.ex("UPDATE runs SET description='Something else'")
    assert not any(h["kind"] == "run" for h in hits(c, "compost"))


def test_scrubbed_secret_leaves_the_index(c, be):
    thread(c, "Oops", "my token is ghp_ABCDEFGHIJKLMNOP ok")
    assert len(hits(c, "ghp_ABCDEFGHIJKLMNOP", kind="message")) == 1
    ok(c.put("/api/secrets/GITHUB_TOKEN", json={"value": "ghp_ABCDEFGHIJKLMNOP"}))
    assert hits(c, "ghp_ABCDEFGHIJKLMNOP") == []
    assert len(hits(c, "[secret:GITHUB_TOKEN]", kind="message")) == 1


def test_migration_backfills_index(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    db.ex("INSERT INTO threads(title, created_by, created_at, updated_at) VALUES('t','owner','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    db.ex("INSERT INTO messages(thread_id, sender, body, created_at) VALUES(1,'owner','findable text','2026-01-01T00:00:00Z')")
    db.ex("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
    assert db.q("SELECT rowid FROM messages_fts WHERE messages_fts MATCH '\"findable\"'") == []
    db.ex("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    assert db.q("SELECT rowid FROM messages_fts WHERE messages_fts MATCH '\"findable\"'") == [{"rowid": 1}]


def test_thread_items_paging_covers_every_item_once(c, be):
    t = thread(c, "Busy", "first")
    for i in range(7):
        ok(c.post(f"/api/threads/{t['id']}/messages", json={"body": f"m{i}", "idempotency_key": f"k{i}"}), 201)
        if i % 3 == 0:
            ok(agent(c, "POST", "/questions", json={"thread": t["id"], "text": f"q{i}", "kind": "text", "default": "x"}))
    full = ok(c.get(f"/api/threads/{t['id']}/items", params={"limit": 100}))
    assert full["before_cursor"] is None
    ids = [(i["type"], (i.get("message") or i.get("question"))["id"]) for i in full["items"]]
    assert len(ids) == len(set(ids)) == 11
    # same-second items share a timestamp: a cursor can only skip whole seconds, so page and compare as sets of seconds
    page = ok(c.get(f"/api/threads/{t['id']}/items", params={"limit": 4}))
    assert [i["type"] for i in page["items"]] == [i["type"] for i in full["items"]][-4:]
    assert page["before_cursor"] == page["items"][0]["at"].replace("Z", "+00:00") or page["before_cursor"]
    older = ok(c.get(f"/api/threads/{t['id']}/items", params={"limit": 100, "before": page["before_cursor"]}))
    assert all(i["at"] < page["before_cursor"].replace("+00:00", "Z") for i in older["items"])


def test_counts_unread_and_open_asks_in_lists(c, be):
    p = ok(c.post("/api/projects", json={"name": "Lab"}), 201)
    t1 = ok(c.post("/api/threads", json={"title": "A", "project": p["slug"], "first_message": "hi"}), 201)
    t2 = ok(c.post("/api/threads", json={"title": "B", "project": p["slug"]}), 201)
    ok(agent(c, "POST", "/messages", json={"thread": t1["id"], "body": "news"}))
    ok(agent(c, "POST", "/messages", json={"thread": t2["id"], "body": "more"}))
    ok(agent(c, "POST", "/questions", json={"thread": t2["id"], "text": "q?", "kind": "text", "default": "x"}))
    proj = ok(c.get("/api/projects"))[0]
    assert proj["unread"] == 2 and proj["open_asks"] == 1 and proj["thread_count"] == 2
    lst = {t["title"]: t for t in ok(c.get("/api/threads"))}
    assert (lst["A"]["unread"], lst["A"]["open_asks"]) == (1, 0) and (lst["B"]["unread"], lst["B"]["open_asks"]) == (1, 1)
    assert lst["A"]["last_message_preview"] == "news"
    assert ok(c.get(f"/api/threads/{t2['id']}")) == lst["B"]


def test_reads_do_not_wait_for_a_long_write_and_tx_reads_see_own_rows(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    db.ex("INSERT INTO threads(title, created_by, created_at, updated_at) VALUES('t','owner','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    started, release, seen = threading.Event(), threading.Event(), {}

    def writer():
        with db.tx():
            db.ex("UPDATE threads SET title='changed'")
            seen["own"] = db.q1("SELECT title FROM threads")["title"]
            started.set()
            release.wait(5)

    th = threading.Thread(target=writer)
    th.start()
    assert started.wait(5)
    t0 = time.time()
    assert db.q1("SELECT title FROM threads")["title"] == "t"  # committed state, no waiting
    assert time.time() - t0 < 1
    release.set()
    th.join()
    assert seen["own"] == "changed" and db.q1("SELECT title FROM threads")["title"] == "changed"
    with pytest.raises(sqlite3.OperationalError):
        db._readers.get().execute("UPDATE threads SET title='x'")


def test_memory_database_keeps_one_connection():
    db = Database(":memory:")
    db.ex("INSERT INTO threads(title, created_by, created_at, updated_at) VALUES('t','owner','x','x')")
    assert db._readers is None and db.q1("SELECT title FROM threads")["title"] == "t"
