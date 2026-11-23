"""H5 part 2: the agent's side of friends, presence and waking (friends.py, presence.py, api/agent.py), against the fake gateway of
test_friends.py. The agent talks to control with its token; the owner is the cookie client."""

# ruff: noqa: F811  (the `w` fixture is imported from test_friends.py)
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from l0w_control.auth import iso, utcnow

from .test_friends import BOB, BOB_FP, World, _wait, ok, sent, w  # noqa: F401

AGENT = {"X-Agent-Token": "agent-secret"}
ANA = "ana@hub.example"
SECRET = "sk-live/abc+123=XYZ_9876"


def agent(w: World) -> TestClient:
    """The agent: its token and no owner session."""
    return TestClient(w.c.app, client=("192.168.1.21", 5000), headers=AGENT)


def sends(w: World, kind: str, to: str | None = None) -> list[dict]:
    return [b for b in w.gw.bodies("/v0/peer/send") if b["kind"] == kind and (to is None or b["to"] == to)]


def add_friend(w: World, address: str, **fields) -> int:
    tid = w.befriend(address)
    if fields:
        w.be.db.ex(f"UPDATE friends SET {', '.join(f'{k}=?' for k in fields)} WHERE address=?", (*fields.values(), address))
    return tid


def wakeup(w: World, at: datetime, status: str = "planned") -> int:
    return w.be.db.ex("INSERT INTO wakeups(provider, at, budget, tier, reason, kind, created_by, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      ("claude-subscription", iso(at), 5, "sonnet/medium", "tidy", "planned", "assistant", status, w.be.db.now(), w.be.db.now()))


# ── messages to a friend ───────────────────────────────────────────────────
def test_an_assistant_post_in_a_peer_thread_is_sent_redacted(w):
    tid = w.befriend()
    w.be.vault.set("MY_KEY", SECRET)
    m = ok(agent(w).post("/api/agent/messages", json={"thread": tid, "body": f"the key is {SECRET}"}))
    assert m["sender"] == "assistant" and m["delivery"] == "queued" and SECRET not in m["body"]
    w.run(w.be.friends_.flush())
    send = sent(w)[0]
    assert send["to"] == BOB and send["kind"] == "message"
    assert send["body"] == {"thread": str(tid), "reply_to": None, "text": "the key is [secret:MY_KEY]", "files": []}
    assert SECRET not in json.dumps(w.gw.calls)
    # posting started a flush in the background: let it finish, or the reset below finds the row still in flight
    assert _wait(lambda: not w.be.friends_._inflight, 5.0)
    ob = w.be.db.q1("SELECT * FROM peer_outbox")
    assert ob["message_id"] == m["id"] and ob["state"] == "queued"
    # A secret stored after the message was written is still redacted at send time (redaction runs at both ends).
    w.be.db.ex("UPDATE messages SET body='later secret tok-9999-aaaa' WHERE id=?", (m["id"],))
    w.be.db.ex("UPDATE peer_outbox SET state='pending', envelope_id=NULL WHERE id=?", (ob["id"],))
    w.be.vault.set("OTHER", "tok-9999-aaaa")
    w.run(w.be.friends_.flush())
    assert _wait(lambda: len(w.gw.bodies("/v0/peer/send")) > 1, 5.0)
    assert w.gw.bodies("/v0/peer/send")[1]["body"]["text"] == "later secret [secret:OTHER]"
    # The outbox reports delivery; a failure is told to the owner and, for its own message, to the agent.
    w.gw.outbox = [{"id": "out-2", "to": BOB, "kind": "message", "state": "failed", "error": "no_token", "updated": "2026-11-22T10:05:00Z"}]
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert any("Your message" in n["body"] and "wasn't delivered" in n["body"] for n in w.be.db.q("SELECT body FROM agent_notifications"))
    assert "wasn't delivered" in ok(agent(w).get("/api/agent/inbox"))["text"]


def test_paused_blocked_or_unfriended_means_nothing_is_stored_or_sent(w):
    tid = w.befriend()
    a = agent(w)
    ok(w.c.patch("/api/friends/settings", json={"paused": True}))
    r = a.post("/api/agent/messages", json={"thread": tid, "body": "hi"})
    assert r.status_code == 409 and "paused" in r.json()["detail"]
    ok(w.c.patch("/api/friends/settings", json={"paused": False}))
    ok(w.c.post(f"/api/friends/{BOB}/block"))
    r = a.post("/api/agent/messages", json={"thread": tid, "body": "hi"})
    assert r.status_code == 409 and "blocked" in r.json()["detail"]
    w.be.db.ex("DELETE FROM friends WHERE address=?", (BOB,))
    r = a.post("/api/agent/messages", json={"thread": tid, "body": "hi"})
    assert r.status_code == 409 and "not friends" in r.json()["detail"]
    assert w.be.db.q("SELECT 1 FROM messages WHERE sender='assistant'") == []  # nothing stored
    w.run(w.be.friends_.flush())
    assert sends(w, "message") == []
    # An ordinary thread is untouched.
    n = ok(w.c.post("/api/threads", json={"title": "Plain"}), 201)
    ok(a.post("/api/agent/messages", json={"thread": n["id"], "body": "private"}))
    w.run(w.be.friends_.flush())
    assert sends(w, "message") == []


# ── what the agent can see and do about friends ────────────────────────────
def test_the_agent_sees_friends_without_secrets_and_cannot_decide_requests(w):
    tid = add_friend(w, BOB, share="shared", display_name="Bobby")
    w.arrive(BOB, "presence", {"state": "working", "next": "tonight", "now": "fixing the pump"}, env_id="p1")
    ok(w.c.post("/api/friends/codes", json={"label": "for Ana"}), 201)
    w.arrive("eve@other.example", "friend-request", {"code": None, "note": "hi", "by": "owner", "token": {"x": 1}})  # an owner card: never the agent's business
    w.be.db.ex("INSERT INTO friends(address, display_name, state, created_at, updated_at) VALUES('mal@other.example','mal@other.example','blocked',?,?)", (w.be.db.now(),) * 2)
    seen = ok(agent(w).get("/api/agent/friends"))["friends"]
    assert [x["address"] for x in seen] == [BOB]
    assert seen[0] == {"address": BOB, "name": "Bobby", "state": "friends", "share": "shared", "thread": tid,
                       "presence": {"state": "working", "next": "tonight", "now": "fixing the pump", "at": "2026-11-22T10:00:00Z"}}
    blob = json.dumps(seen)
    assert BOB_FP not in blob and "token" not in blob and "code" not in blob and "l0wf1" not in blob and "fingerprint" not in blob

    # It can draft a request. That is a card for the owner; nothing is sent, and it has no way to decide one.
    a = agent(w)
    d = ok(a.post("/api/agent/friends/requests", json={"address": ANA, "note": f"my assistant says hi {SECRET}"}))
    assert d["state"] == "pending" and d["address"] == ANA
    assert "/v0/peer/send" not in w.gw.paths()
    assert [x["address"] for x in ok(agent(w).get("/api/agent/friends"))["friends"]] == [BOB, ANA]
    assert ok(agent(w).get("/api/agent/friends"))["friends"][1]["state"] == "draft"
    card = next(r for r in ok(w.c.get("/api/friends/requests")) if r["address"] == ANA)
    assert card["made_by"] == "assistant" and card["state"] == "pending"
    assert any(n["kind"] == "approval" for n in w.be.db.q("SELECT kind FROM notifications"))
    assert a.post("/api/agent/friends/requests", json={"address": ANA}).status_code == 409  # not twice
    assert a.post("/api/agent/friends/requests", json={"address": BOB}).status_code == 409  # already friends
    assert a.post("/api/agent/friends/requests", json={"address": "nonsense"}).status_code in (400, 422)
    for path in (f"/api/agent/friends/requests/{d['id']}/approve", f"/api/agent/friends/requests/{d['id']}/decline", f"/api/agent/friends/requests/{d['id']}/send"):
        assert a.post(path).status_code in (404, 405)
    for path in (f"/api/friends/requests/{d['id']}/approve", f"/api/friends/requests/{d['id']}/decline", "/api/friends", "/api/friends/settings"):
        assert agent(w).request("POST" if "requests" in path else "GET", path, headers=AGENT).status_code in (401, 403), path
    # From the sandbox network the agent token reaches only /api/agent/*.
    sandbox = TestClient(w.c.app, client=("10.255.255.5", 5000), headers=AGENT)
    assert sandbox.post(f"/api/friends/requests/{d['id']}/approve").status_code in (401, 403)
    assert sandbox.get("/api/agent/friends").status_code == 200
    assert "/v0/peer/send" not in w.gw.paths()
    # The owner approves it: only then is it sent, and the note was redacted.
    w.be.vault.set("MY_KEY", SECRET)
    ok(w.c.post(f"/api/friends/requests/{d['id']}/approve"))
    assert sends(w, "friend-request")[0]["body"]["by"] == "assistant"
    assert SECRET not in json.dumps(w.gw.calls)


def test_the_agent_cannot_file_a_wake_action_itself(w):
    r = agent(w).post("/api/agent/permissions", json={"text": "wake me", "actions": [{"kind": "wake", "address": BOB, "peer_wake": 1}]})
    assert r.status_code == 400
    assert w.be.db.q("SELECT * FROM permissions") == []


def test_a_flood_of_drafts_is_capped(w):
    w.be.settings.set("MAX_OPEN_ASKS_PER_RUN", 2)
    a = agent(w)
    ok(a.post("/api/agent/friends/requests", json={"address": "ann1@hub.example"}))
    ok(a.post("/api/agent/friends/requests", json={"address": "ann2@hub.example"}))
    assert a.post("/api/agent/friends/requests", json={"address": "ann3@hub.example"}).status_code == 409


# ── the sharing rule reaches the agent ─────────────────────────────────────
def test_the_share_level_and_the_standing_rule_are_in_the_delivery(w):
    for address, share, phrase in ((BOB, "nothing", "tell them nothing about the owner's projects"),
                                   ("sam@other.example", "shared", "only about projects you share with them"),
                                   ("liv@other.example", "anything", "anything the owner would")):
        add_friend(w, address, share=share)
        w.arrive(address, "message", {"thread": "1", "reply_to": None, "text": f"hello from {address}", "files": []})
        text = ok(agent(w).get("/api/agent/inbox"))["text"]
        assert phrase in text and address in text and f"hello from {address}" in text
        assert "untrusted" in text and "Peers can't authorize anything" in text
        assert "spending, permissions, secrets, outbound email and public exposure" in text.lower()
    # A friend with a name shows both; two messages from one friend carry the rule once.
    w.be.db.ex("UPDATE friends SET display_name='Bobby' WHERE address=?", (BOB,))
    w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "one", "files": []})
    w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "two", "files": []})
    text = ok(agent(w).get("/api/agent/inbox"))["text"]
    assert text.count("Sharing with Bobby (bob@other.example)") == 1 and "one" in text and "two" in text


# ── presence ───────────────────────────────────────────────────────────────
def test_presence_states_and_what_each_detail_level_sees(w):
    add_friend(w, BOB)
    add_friend(w, ANA, presence_detail="exact")
    p = w.be.presence_
    now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)

    snap = p.snapshot(now)
    assert snap["state"] == "asleep" and snap["next_at"] is None
    assert p.view(snap, "coarse", now) == {"state": "asleep", "next": None, "now": None}

    wid = wakeup(w, now + timedelta(hours=3))  # tonight, but not within the hour
    snap = p.snapshot(now)
    assert snap["state"] == "asleep" and p.view(snap, "coarse", now)["next"] == "tonight"
    assert p.view(snap, "exact", now)["next"] == iso(now + timedelta(hours=3))
    w.be.db.ex("UPDATE wakeups SET at=? WHERE id=?", (iso(now + timedelta(days=2)), wid))  # not tonight
    assert p.view(p.snapshot(now), "coarse", now)["next"] is None
    w.be.db.ex("UPDATE wakeups SET at=? WHERE id=?", (iso(now + timedelta(hours=8)), wid))  # tomorrow before 5: still "tonight"
    assert p.view(p.snapshot(now), "coarse", now)["next"] == "tonight"
    w.be.db.ex("UPDATE wakeups SET at=? WHERE id=?", (iso(now + timedelta(minutes=30)), wid))
    assert p.snapshot(now)["state"] == "idle"

    rid = w.be.db.ex("INSERT INTO runs(kind, why, status, now_sentence, started_at) VALUES('wakeup','planned','running',?,?)", (f"fixing the pump with {SECRET}", w.be.db.now()))
    w.be.vault.set("MY_KEY", SECRET)
    snap = p.snapshot(now)
    assert snap["state"] == "working"
    assert p.view(snap, "coarse", now)["now"] is None  # the sentence is for exact friends only
    seen = p.view(snap, "exact", now)
    assert seen["now"] == "fixing the pump with [secret:MY_KEY]" and seen["state"] == "working"
    assert rid


def test_presence_is_sent_on_change_exact_only_to_exact_friends(w):
    add_friend(w, BOB)
    add_friend(w, ANA, presence_detail="exact")
    p = w.be.presence_
    t0 = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    soon = t0 + timedelta(hours=3)
    wakeup(w, soon)

    w.run(p.tick(t0))
    first = sends(w, "presence")
    assert {b["to"] for b in first} == {BOB, ANA}
    assert next(b for b in first if b["to"] == BOB)["body"] == {"state": "asleep", "next": "tonight", "now": None}
    assert next(b for b in first if b["to"] == ANA)["body"] == {"state": "asleep", "next": iso(soon), "now": None}
    w.run(p.tick(t0 + timedelta(minutes=1)))
    assert len(sends(w, "presence")) == 2  # unchanged: nothing more

    # A run starts: the state changed, so both hear at once; only the exact friend gets the sentence.
    rid = w.be.db.ex("INSERT INTO runs(kind, why, status, now_sentence, started_at) VALUES('wakeup','planned','running','Reading the backlog',?)", (w.be.db.now(),))
    w.run(p.tick(t0 + timedelta(minutes=2)))
    new = sends(w, "presence")[2:]
    assert next(b for b in new if b["to"] == BOB)["body"] == {"state": "working", "next": "tonight", "now": None}
    assert next(b for b in new if b["to"] == ANA)["body"]["now"] == "Reading the backlog"

    # Only the sentence changes: held back until the interval (a setting) has passed.
    w.be.db.ex("UPDATE runs SET now_sentence='Writing the report' WHERE id=?", (rid,))
    w.run(p.tick(t0 + timedelta(minutes=5)))
    assert len(sends(w, "presence")) == 4
    w.be.settings.set("PRESENCE_INTERVAL_MINUTES", 10)
    w.run(p.tick(t0 + timedelta(minutes=13)))
    last = sends(w, "presence")[-1]
    assert len(sends(w, "presence")) == 5 and last["to"] == ANA and last["body"]["now"] == "Writing the report"

    # The run ends: back to asleep for both.
    w.be.db.ex("UPDATE runs SET status='done', ended_at=? WHERE id=?", (w.be.db.now(), rid))
    w.run(p.tick(t0 + timedelta(minutes=14)))
    assert {(b["to"], b["body"]["state"]) for b in sends(w, "presence")[5:]} == {(BOB, "asleep"), (ANA, "asleep")}


def test_presence_is_not_sent_while_paused_or_to_people_who_are_not_friends(w):
    add_friend(w, BOB)
    w.be.db.ex("INSERT INTO friends(address, display_name, state, created_at, updated_at) VALUES('mal@other.example','mal','blocked',?,?)", (w.be.db.now(),) * 2)
    w.be.db.ex("INSERT INTO friends(address, display_name, state, created_at, updated_at) VALUES('eve@other.example','eve','requested_in',?,?)", (w.be.db.now(),) * 2)
    p, t0 = w.be.presence_, datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    ok(w.c.patch("/api/friends/settings", json={"paused": True}))
    w.run(p.tick(t0))
    assert sends(w, "presence") == []
    ok(w.c.patch("/api/friends/settings", json={"paused": False}))
    w.run(p.tick(t0))
    assert [b["to"] for b in sends(w, "presence")] == [BOB]  # a muted friend would also hear: muting is inbound only
    w.be.db.ex("UPDATE friends SET muted=1 WHERE address=?", (BOB,))
    wakeup(w, t0 + timedelta(minutes=20))
    w.run(p.tick(t0 + timedelta(minutes=1)))
    assert [b["body"]["state"] for b in sends(w, "presence")] == ["asleep", "idle"]


def test_a_refused_presence_is_retried_later_not_every_tick(w):
    add_friend(w, BOB)
    p, t0 = w.be.presence_, datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    w.gw.errors["/v0/peer/send"] = (400, "no_such_member", "gone")
    w.run(p.tick(t0))
    w.run(p.tick(t0 + timedelta(seconds=15)))
    assert len(w.gw.bodies("/v0/peer/send")) == 1  # one try, then it waits a minute
    w.run(p.tick(t0 + timedelta(minutes=2)))
    assert len(w.gw.bodies("/v0/peer/send")) == 2 and w.be.db.q1("SELECT presence_sent_json FROM friends")["presence_sent_json"]


# ── waking ─────────────────────────────────────────────────────────────────
def replies(w: World, outcome: str | None = None) -> list[dict]:
    return [b["body"] for b in sends(w, "wake-reply") if outcome is None or b["body"]["outcome"] == outcome]


def test_waking_never_answers_denied_and_does_not_bother_the_owner(w):
    add_friend(w, BOB)
    w.arrive(BOB, "wake-request", {"reason": "need the test results"}, env_id="wk-1")
    assert replies(w) == [{"request": "wk-1", "outcome": "denied"}]
    assert w.be.db.q("SELECT * FROM wakeups") == [] and w.note() == [] and w.be.db.q("SELECT * FROM permissions") == []


def test_waking_ask_goes_to_the_owner_who_allows_or_denies(w):
    tid = add_friend(w, BOB, waking="ask")
    w.arrive(BOB, "wake-request", {"reason": f"need the results {SECRET}"}, env_id="wk-1")
    assert replies(w) == []  # waiting for the owner
    cards = ok(w.c.get("/api/inbox"))
    perm = next(e for e in cards if e["kind"] == "permission")["permission"]
    assert perm["actions"][0]["kind"] == "wake" and perm["scopes"] == ["once"] and BOB in perm["actions"][0]["summary"] and perm["thread_id"] == tid
    assert "need the results" in perm["reason"] and perm["status"] == "open"
    assert any(n["kind"] == "approval" and "asks to wake" in n["title"] for n in w.be.db.q("SELECT * FROM notifications"))
    assert w.be.db.q("SELECT * FROM wakeups") == []
    # It isn't one of the agent's asks: it can neither see it nor withdraw it, and the decision isn't delivered to it.
    assert agent(w).post(f"/api/agent/permissions/{perm['id']}/withdraw", json={}).status_code == 409
    assert all(x["id"] != perm["id"] for x in ok(agent(w).get("/api/agent/asks")) if x["kind"] == "permission")

    ok(w.c.post(f"/api/permissions/{perm['id']}/decide", json={"decision": "allow", "scope": "once"}))
    wk = w.be.db.q("SELECT * FROM wakeups")
    assert len(wk) == 1 and wk[0]["kind"] == "planned" and wk[0]["created_by"] == "control" and wk[0]["status"] == "planned"
    assert wk[0]["budget"] == 5 and BOB in wk[0]["reason"] and "untrusted" in wk[0]["reason"] and "need the results" in wk[0]["reason"]
    assert wk[0]["at"] > iso(utcnow())  # soon, not now
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert replies(w) == [{"request": "wk-1", "outcome": "allowed"}]
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert len(replies(w)) == 1  # said once
    assert "Permission" not in ok(agent(w).get("/api/agent/inbox"))["text"]
    assert ok(w.c.get("/api/friends"))[0]["waking"] == "ask"

    # A second request, denied.
    w.arrive(BOB, "wake-request", {"reason": "again"}, env_id="wk-2")
    perm2 = next(e for e in ok(w.c.get("/api/inbox")) if e["kind"] == "permission")["permission"]
    ok(w.c.post(f"/api/permissions/{perm2['id']}/decide", json={"decision": "deny"}))
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert replies(w)[-1] == {"request": "wk-2", "outcome": "denied"} and len(w.be.db.q("SELECT * FROM wakeups")) == 1


def test_waking_ask_is_limited_per_friend_per_day(w):
    add_friend(w, BOB, waking="ask")
    w.be.settings.set("WAKE_ASK_LIMIT_PER_DAY", 2)
    for n in range(3):
        w.arrive(BOB, "wake-request", {"reason": f"try {n}"}, env_id=f"wk-{n}")
    open_cards = w.be.db.q("SELECT * FROM permissions WHERE status='open'")
    assert len(open_cards) == 2
    assert replies(w) == [{"request": "wk-2", "outcome": "later"}]  # the third wasn't asked about
    # Another friend has their own allowance.
    add_friend(w, ANA, waking="ask")
    w.arrive(ANA, "wake-request", {"reason": "hi"}, env_id="ana-1")
    assert len(w.be.db.q("SELECT * FROM permissions WHERE status='open'")) == 3
    # Yesterday's asks don't count.
    w.be.db.ex("UPDATE peer_wakes SET created_at=? WHERE address=?", (iso(utcnow() - timedelta(days=2)), BOB))
    w.arrive(BOB, "wake-request", {"reason": "next day"}, env_id="wk-9")
    assert len(w.be.db.q("SELECT * FROM permissions WHERE status='open'")) == 4


def test_an_unanswered_wake_request_expires_to_later(w):
    add_friend(w, BOB, waking="ask")
    w.arrive(BOB, "wake-request", {"reason": "please"}, env_id="wk-1")
    w.be.settings.set("WAKE_ASK_EXPIRE_HOURS", 2)
    w.run(w.be.friends_.housekeeping(utcnow() + timedelta(hours=1)))
    assert replies(w) == [] and w.be.db.q1("SELECT status FROM permissions")["status"] == "open"
    w.run(w.be.friends_.housekeeping(utcnow() + timedelta(hours=3)))
    assert replies(w) == [{"request": "wk-1", "outcome": "later"}] and w.be.db.q1("SELECT status FROM permissions")["status"] == "withdrawn"


def test_waking_allow_plans_a_wakeup_at_once_and_counts_against_the_budget(w):
    add_friend(w, BOB, waking="allow")
    w.arrive(BOB, "wake-request", {"reason": "need the results"}, env_id="wk-1")
    assert replies(w) == [{"request": "wk-1", "outcome": "allowed"}] and w.be.db.q("SELECT * FROM permissions") == []
    wk = w.be.db.q("SELECT * FROM wakeups")
    assert len(wk) == 1 and wk[0]["budget"] == 5
    assert w.be.budgets.scheduled("claude-subscription", utcnow(), utcnow() + timedelta(days=8)) >= 5  # it's in the plan the budget counts
    assert [n["title"] for n in w.note()] == [f"{BOB} woke your assistant"]
    # Asking again while that wakeup is still planned doesn't plan a second one.
    w.arrive(BOB, "wake-request", {"reason": "and again"}, env_id="wk-2")
    assert replies(w)[-1] == {"request": "wk-2", "outcome": "allowed"} and len(w.be.db.q("SELECT * FROM wakeups")) == 1
    # The wakeup's brief says who asked (the scheduler hands the reason to the run).
    from l0w_control.runs.manager import RunManager

    assert "your planned wakeup: {detail}" in RunManager.WHY["planned"]


def test_a_wakeup_that_does_not_fit_the_budget_is_answered_later(w):
    add_friend(w, BOB, waking="allow")
    w.be.settings.set("WAKE_FRIEND_PTS", 1000)
    w.arrive(BOB, "wake-request", {"reason": "big"}, env_id="wk-1")
    assert replies(w) == [{"request": "wk-1", "outcome": "later"}] and w.be.db.q("SELECT * FROM wakeups") == []


def test_wake_requests_are_refused_while_paused_and_a_held_reply_follows(w):
    add_friend(w, BOB, waking="ask")
    w.arrive(BOB, "wake-request", {"reason": "x"}, env_id="wk-1")
    perm = next(e for e in ok(w.c.get("/api/inbox")) if e["kind"] == "permission")["permission"]
    ok(w.c.post(f"/api/permissions/{perm['id']}/decide", json={"decision": "allow", "scope": "once"}))
    ok(w.c.patch("/api/friends/settings", json={"paused": True}))
    assert w.arrive(BOB, "wake-request", {"reason": "y"}, env_id="wk-2").status_code == 503  # refused at the door: they retry later
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert replies(w) == []  # the answer waits while peer activity is paused
    ok(w.c.patch("/api/friends/settings", json={"paused": False}))
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert replies(w) == [{"request": "wk-1", "outcome": "allowed"}]


def test_the_assistant_can_ask_a_friend_to_wake_and_hears_the_answer(w):
    add_friend(w, BOB)
    a = agent(w)
    w.be.vault.set("MY_KEY", SECRET)
    ok(a.post(f"/api/agent/friends/{BOB}/wake", json={"reason": f"results ready {SECRET}"}))
    sent = sends(w, "wake-request", BOB)
    assert len(sent) == 1 and sent[0]["body"] == {"reason": "results ready [secret:MY_KEY]"}
    out = w.be.db.q1("SELECT * FROM peer_wakes WHERE direction='out'")
    assert out["address"] == BOB and out["outcome"] is None
    # A reply to something we never asked is ignored for the agent; the real answer reaches it as a note.
    w.arrive(BOB, "wake-reply", {"request": "nope", "outcome": "allowed"}, env_id="r1")
    assert "answered your wake request" not in ok(agent(w).get("/api/agent/inbox"))["text"]
    w.arrive(BOB, "wake-reply", {"request": out["envelope_id"], "outcome": "denied"}, env_id="r2")
    text = ok(agent(w).get("/api/agent/inbox"))["text"]
    assert "answered your wake request: denied" in text
    assert w.be.db.q1("SELECT outcome FROM peer_wakes WHERE direction='out'")["outcome"] == "denied"
    # Limited per friend per day, and only to friends.
    w.be.settings.set("WAKE_SEND_LIMIT_PER_DAY", 1)
    assert a.post(f"/api/agent/friends/{BOB}/wake", json={"reason": "again please"}).status_code == 409
    assert a.post("/api/agent/friends/zed@other.example/wake", json={"reason": "hello there"}).status_code == 409
    ok(w.c.patch("/api/friends/settings", json={"paused": True}))
    w.be.settings.set("WAKE_SEND_LIMIT_PER_DAY", 5)
    assert a.post(f"/api/agent/friends/{BOB}/wake", json={"reason": "paused now"}).status_code == 409


# ── settings ───────────────────────────────────────────────────────────────
def test_the_new_friend_settings_are_declared_and_used(w):
    from l0w_control.config import ADVANCED, SPECS

    for key, default in {"PRESENCE_INTERVAL_MINUTES": 10, "WAKE_ASK_LIMIT_PER_DAY": 3, "WAKE_ASK_EXPIRE_HOURS": 24, "WAKE_FRIEND_PTS": 5.0,
                         "WAKE_SEND_LIMIT_PER_DAY": 3, "FRIEND_CODE_EXPIRE_DAYS": 14, "PEER_PENDING_GIVE_UP_HOURS": 24}.items():
        assert SPECS[key].default == default and ADVANCED[key][0] == "Friends" and SPECS[key].desc.endswith(".")
    # A new code lasts as long as the setting says, unless the owner picks a time.
    w.be.settings.set("FRIEND_CODE_EXPIRE_DAYS", 3)
    made = ok(w.c.post("/api/friends/codes", json={}), 201)
    assert abs((datetime.fromisoformat(made["expires_at"]) - datetime.now(UTC)).total_seconds() - 3 * 86400) < 120
    made = ok(w.c.post("/api/friends/codes", json={"expires_days": 1}), 201)
    assert abs((datetime.fromisoformat(made["expires_at"]) - datetime.now(UTC)).total_seconds() - 86400) < 120
    # A message the gateway never took is given up on after the configured hours.
    tid = w.befriend()
    w.gw.down = True
    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "hello", "idempotency_key": "m-1"}), 201)
    w.run(w.be.friends_.flush())
    w.be.settings.set("PEER_PENDING_GIVE_UP_HOURS", 2)
    w.be.friends_.now = lambda: utcnow() + timedelta(hours=1)
    w.run(w.be.friends_.flush())
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "pending"
    w.be.friends_.now = lambda: utcnow() + timedelta(hours=3)
    w.gw.down = False
    w.run(w.be.friends_.flush())
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "failed"


def test_tunnel_callers_cannot_reach_the_agents_friend_routes(w):
    t = TestClient(w.c.app, client=("10.255.254.9", 40000), headers=AGENT)
    assert t.get("/api/agent/friends").status_code in (401, 403, 404)
