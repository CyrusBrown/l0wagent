"""H5: the spend breaker for peer activity (breaker.py), against the fake gateway of test_friends.py. Time is a fake clock; the
usage ledger is written by hand (what a run spent is what the test says)."""

# ruff: noqa: F811  (the `w` fixture is imported from test_friends.py)
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from l0w_control.auth import iso

from .test_friends import BOB, World, ok, w  # noqa: F401

AGENT = {"X-Agent-Token": "agent-secret"}
ANA = "ana@hub.example"
NOON = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)  # the owner's time zone is UTC in the test world


def agent(w: World) -> TestClient:
    return TestClient(w.c.app, client=("192.168.1.21", 5000), headers=AGENT)


@pytest.fixture
def clock(w):
    box = [NOON]
    w.be.friends_.now = lambda: box[0]
    w.be.budgets.now = lambda: box[0]
    return box


def mkrun(w: World, budget: float = 40, status: str = "running", wakeup_id: int | None = None, started: datetime = NOON) -> int:
    return w.be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, wakeup_id) VALUES('wakeup','planned','claude-subscription','sonnet/medium',?,?,?,?)",
                      (budget, status, iso(started), wakeup_id))


def spend(w: World, run_id: int, pts: float) -> None:
    w.be.db.ex("INSERT INTO usage_ledger(provider, run_id, who, bucket, pts, at, updated_at) VALUES('claude-subscription',?,'assistant','allowance',?,?,?)",
               (run_id, pts, w.be.db.now(), w.be.db.now()))


def hear(w: World, run_id: int, address: str = BOB, n: int = 0) -> None:
    """A friend's message arrives and is delivered into the run (what the agent's inbox check does)."""
    w.befriend(address) if not w.be.db.q1("SELECT 1 FROM friends WHERE address=?", (address,)) else None
    w.arrive(address, "message", {"thread": "1", "reply_to": None, "text": f"ping {n}", "files": []}, env_id=f"m-{address}-{run_id}-{n}-{w.n}")
    w.be.conv.deliver(run_id=run_id)


def b(w: World):
    return w.be.friends_.breaker


def spent(w: World, run_id: int) -> dict[str, float]:
    return {a: round(v, 6) for a, v in b(w).run_spend(run_id).items()}


def post(w: World, address: str = BOB):
    tid = w.be.friends_._friend_row(address)["thread_id"]
    return agent(w).post("/api/agent/messages", json={"thread": tid, "body": "hello"})


def notes(w: World) -> list[str]:
    return [n["body"] for n in w.be.db.q("SELECT body FROM agent_notifications ORDER BY id")]


def alarms(w: World) -> list[dict]:
    return [n for n in w.be.db.q("SELECT * FROM notifications WHERE kind='friends' ORDER BY id") if n["title"] == "Spending on friends hit a limit"]


def loose(w: World) -> None:
    """Limits out of the way, to test the spans alone."""
    w.be.settings.set("PEER_DAY_PCT", 100)
    w.be.settings.set("PEER_RUN_PCT", 100)


# ── spans ──────────────────────────────────────────────────────────────────
def test_a_span_opens_on_delivery_and_closes_when_the_agent_posts_elsewhere(w, clock):
    loose(w)
    w.befriend()
    run = mkrun(w, 100)
    spend(w, run, 3)
    hear(w, run)
    sp = w.be.db.q1("SELECT * FROM peer_spans")
    assert sp["run_id"] == run and sp["address"] == BOB and sp["start_pts"] == 3 and sp["end_pts"] is None and sp["started_at"] == iso(NOON)
    spend(w, run, 4)
    assert spent(w, run) == {BOB: 4}  # an open span counts up to now
    # Another message while it is open extends it, it doesn't make a second.
    hear(w, run, n=1)
    assert w.be.db.q1("SELECT COUNT(*) AS n FROM peer_spans")["n"] == 1
    # The agent posts in an ordinary thread: the span closes there.
    t = ok(w.c.post("/api/threads", json={"title": "Plain"}), 201)
    ok(agent(w).post("/api/agent/messages", json={"thread": t["id"], "body": "private"}))
    sp = w.be.db.q1("SELECT * FROM peer_spans")
    assert sp["end_pts"] == 7 and sp["closed_by"] == "post"
    spend(w, run, 5)
    assert spent(w, run) == {BOB: 4}  # what came after isn't theirs
    # A later message opens a new span at the new total.
    hear(w, run, n=2)
    assert [r["start_pts"] for r in w.be.db.q("SELECT * FROM peer_spans ORDER BY id")] == [3, 12]


def test_a_span_closes_when_the_run_ends_or_ten_minutes_after_the_last_message(w, clock):
    loose(w)
    w.befriend()
    r1, r2 = mkrun(w, 100), mkrun(w, 100)
    hear(w, r1)
    spend(w, r1, 2)
    w.be.db.ex("UPDATE runs SET status='done' WHERE id=?", (r1,))
    w.run(w.be.friends_.housekeeping(NOON + timedelta(seconds=30)))  # the run ended: housekeeping closes it
    assert w.be.db.q1("SELECT * FROM peer_spans WHERE run_id=?", (r1,))["closed_by"] == "run_end"
    hear(w, r2)
    spend(w, r2, 1)
    clock[0] = NOON + timedelta(minutes=9)
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert w.be.db.q1("SELECT ended_at FROM peer_spans WHERE run_id=?", (r2,))["ended_at"] is None
    hear(w, r2, n=1)  # their next message keeps it going
    clock[0] = NOON + timedelta(minutes=18)
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert w.be.db.q1("SELECT ended_at FROM peer_spans WHERE run_id=?", (r2,))["ended_at"] is None
    spend(w, r2, 1)
    clock[0] = NOON + timedelta(minutes=20)
    w.run(w.be.friends_.housekeeping(clock[0]))
    sp = w.be.db.q1("SELECT * FROM peer_spans WHERE run_id=?", (r2,))
    assert sp["closed_by"] == "timeout" and sp["end_pts"] == 2
    # The finish hook closes a span at once too.
    r3 = mkrun(w, 100)
    hear(w, r3)
    b(w).on_run_end(r3)
    assert w.be.db.q1("SELECT closed_by FROM peer_spans WHERE run_id=?", (r3,))["closed_by"] == "run_end"


def test_friends_whose_spans_overlap_split_the_points_evenly(w, clock):
    loose(w)
    w.befriend()
    w.befriend(ANA)
    run = mkrun(w, 100)
    hear(w, run)  # Bob's span opens at 0
    spend(w, run, 4)
    hear(w, run, ANA)  # Ana's opens at 4
    spend(w, run, 4)
    assert spent(w, run) == {BOB: 6, ANA: 2}  # 4 alone, then 4 shared
    spend(w, run, 2)
    t = ok(w.c.post("/api/threads", json={"title": "Plain"}), 201)
    ok(agent(w).post("/api/agent/messages", json={"thread": t["id"], "body": "x"}))
    assert spent(w, run) == {BOB: 7, ANA: 3}
    spend(w, run, 9)
    assert spent(w, run) == {BOB: 7, ANA: 3}


def test_a_run_a_friend_woke_counts_whole_and_opens_no_spans(w, clock):
    w.befriend()
    wk = w.be.db.ex("INSERT INTO wakeups(provider, at, budget, tier, reason, kind, created_by, status, created_at, updated_at) "
                    "VALUES('claude-subscription',?,5,'sonnet/medium','a friend asked','planned','control','done',?,?)", (iso(NOON), w.be.db.now(), w.be.db.now()))
    w.be.db.ex("INSERT INTO peer_wakes(direction, address, envelope_id, reason, outcome, wakeup_id, created_at) VALUES('in',?,'e1','tests','allowed',?,?)",
               (BOB, wk, w.be.db.now()))
    run = mkrun(w, 5, wakeup_id=wk)
    spend(w, run, 3)
    hear(w, run)
    assert w.be.db.q("SELECT * FROM peer_spans") == []
    assert spent(w, run) == {BOB: 3}
    # 3 of its 5 pts is far over 20% of the run, but a run the owner allowed on its own budget isn't held to the run limit...
    assert b(w).check(run)[0] is None and post(w).status_code == 200
    # ...it counts toward the day's: 5% of a Steady week's 150 pts is 7.5.
    assert b(w).view().day_spent == 3
    spend(w, run, 5)
    r = post(w)
    assert r.status_code == 409 and r.json()["error"] == "peer_breaker"
    assert b(w).view().limit == "day" and b(w).view().day_spent == 8


# ── tripping ───────────────────────────────────────────────────────────────
def test_the_run_limit_refuses_the_agent_tells_it_once_and_asks_the_owner_once(w, clock):
    w.be.settings.set("PEER_DAY_PCT", 100)
    w.befriend()
    run = mkrun(w, 40)  # 20% = 8 pts
    hear(w, run)
    spend(w, run, 7)
    assert post(w).status_code == 200  # still under
    spend(w, run, 2)  # 9 of 8
    r = post(w)
    assert r.status_code == 409 and r.json()["error"] == "peer_breaker"
    assert "run #" in r.json()["detail"] and "nothing was sent" in r.json()["detail"]
    assert len(w.be.db.q("SELECT * FROM messages WHERE sender='assistant'")) == 1  # the refused one was not stored
    assert post(w).status_code == 409  # still refused...
    assert len([n for n in notes(w) if "Spending on friends hit" in n]) == 1  # ...but told once,
    assert len(alarms(w)) == 1  # and the owner asked once
    assert "run" in notes(w)[-1] and "paused until the owner decides" in notes(w)[-1]
    v = ok(w.c.get("/api/friends/breaker"))
    assert v["tripped"] and v["paused"] and v["limit"] == "run" and v["run_id"] == run and v["choices"] == ["allow_today", "allow_run", "stop_today"]
    assert v["run_spent"] == 9 and v["run_limit"] == 8 and f"Run #{run}" in v["explanation"] and v["spent_on"][0]["address"] == BOB
    # Waking is refused too; presence goes on (it isn't the agent's spending).
    r = agent(w).post(f"/api/agent/friends/{BOB}/wake", json={"reason": "need the results"})
    assert r.status_code == 409 and r.json()["error"] == "peer_breaker"
    w.run(w.be.friends_.send_presence(BOB, {"state": "working", "next": None, "now": None}))
    assert [x["kind"] for x in w.gw.bodies("/v0/peer/send")][-1] == "presence"
    # The owner's own messages in the thread are never held.
    tid = w.be.friends_._friend_row(BOB)["thread_id"]
    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "hi from me"}), 201)
    # Housekeeping while it is tripped doesn't ask again.
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert len(alarms(w)) == 1 and len([n for n in notes(w) if "Spending on friends hit" in n]) == 1


def test_a_trip_that_has_gone_away_closes_the_card_and_tells_the_agent(w, clock):
    w.be.settings.set("PEER_DAY_PCT", 100)
    w.befriend()
    run = mkrun(w, 40)
    hear(w, run)
    spend(w, run, 9)
    assert post(w).status_code == 409 and b(w).view().tripped
    w.be.db.ex("UPDATE runs SET status='done' WHERE id=?", (run,))  # the run is over: nothing is left to decide
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert not b(w).view().tripped and "back within the limits" in notes(w)[-1]
    assert post(w).status_code == 200


def test_the_day_limit_trips_across_runs_and_allow_today_raises_it(w, clock):
    w.befriend()
    r1, r2 = mkrun(w, 400), mkrun(w, 400, status="done")  # 20% of 400 is 80: the run limit stays out of it
    hear(w, r2)
    spend(w, r2, 5)
    w.be.db.ex("UPDATE runs SET status='done' WHERE id=?", (r2,))
    b(w).on_run_end(r2)
    hear(w, r1)
    spend(w, r1, 2)
    assert post(w).status_code == 200  # 7 of 7.5 (5% of a Steady 150-pt week)
    spend(w, r1, 2.5)  # 9.5
    r = post(w)
    assert r.status_code == 409 and "tomorrow" in r.json()["detail"]
    v = ok(w.c.get("/api/friends/breaker"))
    assert v["limit"] == "day" and v["choices"] == ["allow_today", "stop_today"] and v["day_spent"] == 9.5 and v["day_limit"] == 7.5 and v["day_pct"] == 5
    assert "tomorrow" in notes(w)[-1] and len(alarms(w)) == 1
    # Allow +5%: the limit is 10% of 150 = 15 pts, and the agent may write again.
    v = ok(w.c.post("/api/friends/breaker/allow-today", json={"idempotency_key": "k1"}))
    assert not v["tripped"] and v["day_limit"] == 15 and v["day_pct"] == 10
    assert post(w).status_code == 200
    assert "allowed 5% more" in notes(w)[-1]
    # The same key again does not raise it twice.
    assert ok(w.c.post("/api/friends/breaker/allow-today", json={"idempotency_key": "k1"}))["day_limit"] == 15
    # Past the new limit, it trips again (a new alarm), and tomorrow the extra is gone.
    spend(w, r1, 6)
    assert post(w).status_code == 409 and len(alarms(w)) == 2
    clock[0] = NOON + timedelta(days=1)
    assert b(w).view().day_limit == 7.5 and b(w).view().day_spent == 0
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert not b(w).view().tripped and "back within the limits" in notes(w)[-1]


def test_allow_for_this_run_exempts_only_that_run(w, clock):
    w.be.settings.set("PEER_DAY_PCT", 100)
    w.befriend()
    r1 = mkrun(w, 40)
    hear(w, r1)
    spend(w, r1, 9)
    assert post(w).status_code == 409
    ok(w.c.post("/api/friends/breaker/allow-run", json={}))
    assert post(w).status_code == 200
    spend(w, r1, 20)
    assert post(w).status_code == 200 and not b(w).view().tripped  # this run is exempt, however much
    assert "run #" in notes(w)[-1] and "allowed" in notes(w)[-1]
    # The next run isn't.
    w.be.db.ex("UPDATE runs SET status='done' WHERE id=?", (r1,))
    r2 = mkrun(w, 40)
    hear(w, r2)
    spend(w, r2, 9)
    assert post(w).status_code == 409 and b(w).view().run_id == r2
    # Allow for this run is only for the run limit.
    w.be.settings.set("PEER_DAY_PCT", 0.5)
    spend(w, r2, 1)
    w.run(w.be.friends_.housekeeping(clock[0]))
    assert b(w).view().limit == "day" and "allow_run" not in b(w).view().choices
    assert w.c.post("/api/friends/breaker/allow-run", json={}).status_code == 409


def test_stop_for_today_holds_until_local_midnight_whatever_the_limits_say(w, clock):
    w.befriend()
    run = mkrun(w, 400)
    hear(w, run)
    spend(w, run, 9)
    assert post(w).status_code == 409
    v = ok(w.c.post("/api/friends/breaker/stop-today", json={}))
    assert v["paused"] and not v["tripped"] and v["stopped_until"] == "2026-11-23T00:00:00Z" and v["choices"] == []
    w.be.settings.set("PEER_DAY_PCT", 100)  # the limits would allow it now
    r = post(w)
    assert r.status_code == 409 and "stopped replies to friends for today" in r.json()["detail"]
    assert agent(w).post(f"/api/agent/friends/{BOB}/wake", json={"reason": "need the results"}).status_code == 409
    # Allow +5% doesn't undo it, and a limit that trips now makes no second card.
    ok(w.c.post("/api/friends/breaker/allow-today", json={}))
    assert post(w).status_code == 409
    clock[0] = datetime(2026, 11, 22, 23, 59, tzinfo=UTC)
    assert post(w).status_code == 409
    clock[0] = datetime(2026, 11, 23, 0, 1, tzinfo=UTC)
    assert post(w).status_code == 200
    assert "stop on replies to friends has ended" in notes(w)[-1]
    assert not b(w).view().paused


def test_the_owners_time_zone_decides_where_the_day_ends(w, clock):
    w.be.db.ex("UPDATE users SET timezone='America/New_York' WHERE id=1")
    w.befriend()
    run = mkrun(w, 400)
    hear(w, run)
    spend(w, run, 9)
    assert post(w).status_code == 409
    v = ok(w.c.post("/api/friends/breaker/stop-today", json={}))
    assert v["stopped_until"] == "2026-11-23T05:00:00Z"  # midnight in New York


# ── a friend's own switch ──────────────────────────────────────────────────
def test_a_friend_with_the_limit_off_is_left_out_and_may_still_be_written_to(w, clock):
    w.be.settings.set("PEER_DAY_PCT", 100)
    w.befriend()
    w.befriend(ANA)
    assert ok(w.c.patch(f"/api/friends/{BOB}", json={"breaker": "off"}))["breaker"] == "off"
    assert {f["address"]: f["breaker"] for f in ok(w.c.get("/api/friends"))} == {BOB: "off", ANA: "normal"}
    assert w.c.patch(f"/api/friends/{BOB}", json={"breaker": "sometimes"}).status_code == 422
    run = mkrun(w, 40)
    hear(w, run)  # only Bob's span
    spend(w, run, 30)
    assert spent(w, run) == {BOB: 30} and b(w).run_spent(run) == 0
    assert post(w).status_code == 200 and not b(w).view().tripped and b(w).view().day_spent == 0
    # Ana is held to it; Bob, being off, can still be written to while it is tripped.
    hear(w, run, ANA)
    spend(w, run, 20)  # shared with Bob's open span: 10 of it is Ana's, over her 8
    assert post(w, ANA).status_code == 409 and b(w).view().tripped
    assert post(w).status_code == 200
    # Switching Bob back on counts what he already cost.
    ok(w.c.patch(f"/api/friends/{BOB}", json={"breaker": "normal"}))
    assert b(w).run_spent(run) > 30 and post(w).status_code == 409


# ── who may do what ────────────────────────────────────────────────────────
def test_only_the_owner_decides(w, clock):
    for path in ("allow-today", "allow-run", "stop-today"):
        assert agent(w).post(f"/api/friends/breaker/{path}", json={}).status_code in (401, 403), path
    assert agent(w).get("/api/friends/breaker").status_code in (401, 403)
    sandbox = TestClient(w.c.app, client=("10.255.255.5", 5000), headers=AGENT)
    assert sandbox.post("/api/friends/breaker/stop-today", json={}).status_code in (401, 403)
    tunnel = w.t.post("/api/friends/breaker/stop-today", json={})
    assert tunnel.status_code in (401, 403, 404)
    assert not b(w).view().paused
