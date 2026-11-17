# ruff: noqa: E501, F811
"""Fixes from the 2026-11-16 review: scheduler resilience, monthly periods, the fallback,
the reviewer's units, owner wakeups' arithmetic, idempotency, retention and DST."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from l0w_control.api import schemas as s
from l0w_control.auth import iso
from l0w_control.backends.base import BadRequest, Conflict
from l0w_control.budgets import in_hours, next_in_hours
from l0w_control.retention import run_retention

from . import test_budgets
from .test_budgets import AGENT, tick, usage

be = test_budgets.be   # the fixtures, shared
c = test_budgets.c

NY = ZoneInfo("America/New_York")


def u(dt: datetime) -> datetime:
    """UTC: comparing across zones is always False inside a repeated hour."""
    return dt.astimezone(UTC)


class Resp:
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code, self._detail = status_code, detail

    def json(self) -> dict:
        return {"detail": self._detail}


def wake(be, at: str, budget: float, tier: str = "sonnet/medium", reason: str = "check the export") -> Resp:
    """The agent's `wake at`, straight to the scheduler."""
    try:
        be.scheduler.plan("claude-subscription", datetime.fromisoformat(at).replace(tzinfo=UTC), budget, tier, reason, by="assistant")
    except BadRequest as e:
        return Resp(400, e.detail)
    return Resp(200)


def go_worker(be):
    """opencode Go (monthly) as the worker."""
    be.budgets.add_api_provider("sk-go-test-0123456789abcdefghij", None, "opencode-go")
    roles = [r.model_copy(update={"provider": "opencode-go", "tier": "sonnet/medium"}) if r.role == "worker" else r for r in be.budgets.roles()]
    be.budgets.set_roles(roles)


# ── C3 ─────────────────────────────────────────────────────────────────────
def test_a_failing_step_doesnt_stop_wakeups_and_is_recorded(c, be):
    wake(be, "2027-03-01T23:00", 20)
    be.clock.t = datetime(2027, 3, 1, 23, 0, 5, tzinfo=UTC)

    def boom(now):
        raise RuntimeError("bad routine")

    be.scheduler.routines_to_plan = boom  # type: ignore[method-assign]
    tick(be)
    tick(be)
    assert len(be.started) == 1   # start_due still ran
    p = be.scheduler.problems_raw()["routines"]
    assert p["count"] == 2 and "bad routine" in p["error"]
    del be.scheduler.routines_to_plan   # the real one again
    tick(be)
    assert be.scheduler.problems_raw() == {}


# ── C5 ─────────────────────────────────────────────────────────────────────
def test_before_reset_routines_follow_the_providers_period(c, be):
    go_worker(be)
    be.clock.t = datetime(2027, 3, 28, 12, tzinfo=UTC)
    tick(be)
    r = be.db.q1("SELECT * FROM routines WHERE builtin='weekly_review'")
    assert r["provider"] == "opencode-go" and r["tier"] == "sonnet/medium"
    w = be.db.q1("SELECT * FROM wakeups WHERE routine_id=? AND status='planned'", (r["id"],))
    assert w and w["at"] == "2027-03-31T18:00:00Z"   # 6 h before the April 1 reset
    assert c.get("/api/routines").json()[0]["schedule_text"] == "6 h before the monthly reset"
    # After this period's time has passed the next one is a month on, not 7 days.
    nxt = be.scheduler.next_occurrence({"kind": "before_reset", "hours": 6}, "opencode-go", datetime(2027, 3, 31, 19, tzinfo=UTC))
    assert nxt == datetime(2027, 4, 30, 18, tzinfo=UTC)
    # Messages say month.
    be.db.ex("INSERT INTO usage_ledger(provider, who, bucket, pts, at, updated_at) VALUES('opencode-go','assistant','allowance',9999,?,?)", (iso(be.clock()), iso(be.clock())))
    wid = be.scheduler._insert("opencode-go", be.clock() + timedelta(minutes=-1), 10, "sonnet/medium", "x", "planned", "assistant")
    asyncio.run(be.scheduler.start_due(be.clock()))
    assert "month's allowance" in be.db.q1("SELECT status_reason FROM wakeups WHERE id=?", (wid,))["status_reason"]


# ── C7 ─────────────────────────────────────────────────────────────────────
def test_a_plan_beyond_48h_keeps_the_fallback_and_its_note_comes_once(c, be):
    tick(be)
    fb = be.db.q1("SELECT * FROM wakeups WHERE kind='fallback' AND status='planned'")
    assert fb
    assert wake(be, "2027-03-04T23:00", 20).status_code == 200   # > 48 h away
    assert be.db.q1("SELECT status FROM wakeups WHERE id=?", (fb["id"],))["status"] == "planned"
    # Cancelled by the owner and replanned: no second note this period.
    be.scheduler.cancel(fb["id"], "owner")
    c.get("/api/agent/inbox", headers=AGENT)
    be.db.ex("UPDATE wakeups SET status='cancelled' WHERE kind='planned'")
    n = be.db.q1("SELECT COUNT(*) AS n FROM agent_notifications WHERE body LIKE '%fallback wakeup%'")["n"]
    tick(be)
    assert be.db.q1("SELECT 1 FROM wakeups WHERE kind='fallback' AND status='planned'")
    assert be.db.q1("SELECT COUNT(*) AS n FROM agent_notifications WHERE body LIKE '%fallback wakeup%'")["n"] == n
    # A wakeup within the 48 h does replace it.
    assert wake(be, "2027-03-02T03:30", 20).status_code == 200
    assert not be.db.q1("SELECT 1 FROM wakeups WHERE kind='fallback' AND status='planned'")


# ── C4 ─────────────────────────────────────────────────────────────────────
def test_the_reviewer_share_and_run_total_are_in_dollars(be):
    from l0w_control.reviewer import Reviewer
    be.budgets.add_api_provider("sk-go-test-0123456789abcdefghij", None, "opencode-go")
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, bucket) VALUES('wakeup','planned','claude-subscription','sonnet/medium',40,'running',?, 'allowance')",
                   (iso(be.clock()),))
    # The worker spent 10 pts ($5 at 2 pts/$); the reviewer, on Go, $1 = 10 Go pts (= 2 Claude pts).
    be.db.ex("INSERT INTO usage_ledger(provider, run_id, who, bucket, cost_usd, pts, at, updated_at) VALUES('claude-subscription',?,'assistant','allowance',5,10,?,?)", (rid, iso(be.clock()), iso(be.clock())))
    be.db.ex("INSERT INTO usage_ledger(provider, run_id, who, bucket, cost_usd, pts, at, updated_at) VALUES('opencode-go',?,'reviewer','allowance',1,10,?,?)", (rid, iso(be.clock()), iso(be.clock())))
    assert be.budgets.run_pts(rid) == pytest.approx(12.0)   # not 20
    rv = Reviewer(be.db, be.settings, be.budgets, be.runner, be.live, be.vault.redact)
    run = be.db.q1("SELECT * FROM runs WHERE id=?", (rid,))
    # Share 25% of 40 pts = 10 Claude pts = $5: $1 is nowhere near it (10 Go pts would have said so).
    assert not rv._out_of_budget(run)
    be.db.ex("UPDATE usage_ledger SET cost_usd=5 WHERE who='reviewer'")
    assert rv._out_of_budget(run)
    # The reviewer's row sits on its own provider, so it counts against that allowance only.
    start, end = be.budgets.week("opencode-go")
    assert be.budgets.spent("opencode-go", start, end) == 10 and be.budgets.spent("claude-subscription", *be.budgets.week("claude-subscription")) == 10


# ── B6 / B7 ────────────────────────────────────────────────────────────────
def test_owner_wakeups_use_the_allowance_arithmetic(c, be):
    r = c.post("/api/wakeups", json={"at": "2027-03-01T20:00:00Z", "budget": 160, "reason": "too big"})
    assert r.status_code == 400 and "Not enough budget" in r.json()["detail"]
    assert c.post("/api/wakeups", json={"at": "2027-03-01T20:00:00Z", "budget": 100, "reason": "ok"}).status_code == 200
    # No assistant limits: 3 on a Steady day and 100 pts is fine, but the total isn't.
    assert c.post("/api/wakeups", json={"at": "2027-03-01T21:00:00Z", "budget": 60, "reason": "no"}).status_code == 400
    # At start it's capped by what's left.
    be.db.ex("INSERT INTO usage_ledger(provider, who, bucket, pts, at, updated_at) VALUES('claude-subscription','assistant','allowance',130,?,?)", (iso(be.clock()), iso(be.clock())))
    be.clock.t = datetime(2027, 3, 1, 20, 0, 5, tzinfo=UTC)
    tick(be)
    assert be.db.q1("SELECT budget FROM runs WHERE id=?", (be.started[-1],))["budget"] == 20


def test_routines_count_in_the_day_and_keep_the_gap_from_any_wakeup(c, be):
    be.budgets.set_budget("claude-subscription", s.BudgetPatch(overrides=s.BudgetLimitsPatch(min_gap_minutes=30)))
    c.post("/api/routines", json={"name": "Inbox", "prompt": "x", "schedule": {"kind": "daily", "time": "23:30"}, "budget": 8})
    tick(be)
    rt = be.db.q1("SELECT at FROM wakeups WHERE kind='routine' AND reason LIKE '%Inbox%'")
    assert rt["at"] == "2027-03-01T23:30:00Z"
    assert "Too close" in wake(be, "2027-03-01T23:15", 10).json()["detail"]
    # Steady allows 2 a day; the routine is one of them.
    assert wake(be, "2027-03-01T22:30", 10).status_code == 200
    r = wake(be, "2027-03-01T22:00", 10)
    assert r.status_code == 400 and "limit is 2 a day" in r.json()["detail"]
    # A routine whose time is taken by a planned wakeup moves past the gap, with a note.
    assert wake(be, "2027-03-02T03:00", 10).status_code == 200
    c.post("/api/routines", json={"name": "Garden", "prompt": "x", "schedule": {"kind": "daily", "time": "03:10"}, "budget": 5})
    tick(be)
    g = be.db.q1("SELECT at, reason FROM wakeups WHERE kind='routine' AND reason LIKE '%Garden%' AND status='planned' ORDER BY at LIMIT 1")
    assert g["at"] == "2027-03-02T03:30:00Z" and "moved from" in g["reason"]
    assert "was moved from" in c.get("/api/agent/inbox", headers=AGENT).json()["text"]


# ── C6 ─────────────────────────────────────────────────────────────────────
def test_idempotency_claims_the_key_first(be):
    calls = []

    class M(BaseModel):
        v: int = 1

    def make():
        calls.append(1)
        # A concurrent retry arrives while the first is still being made.
        with pytest.raises(Conflict) as e:
            be.conv.once("k1", "ep", M, lambda: M())
        assert e.value.error == "in_progress"
        return M()

    first = be.conv.once("k1", "ep", M, make)
    again = be.conv.once("k1", "ep", M, lambda: pytest.fail("ran twice"))
    assert calls == [1] and again == first
    with pytest.raises(Conflict):
        be.conv.once("k1", "other", M, make)
    # A failed first attempt frees the key.
    def fail():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        be.conv.once("k2", "ep", M, fail)
    assert be.conv.once("k2", "ep", M, M) == M()
    # A claim left by a dead attempt is taken over after a minute.
    be.db.ex("INSERT INTO idempotency_keys(key, endpoint, response_json, created_at) VALUES('k3','ep','',?)", (iso(datetime.now(UTC) - timedelta(minutes=5)),))
    assert be.conv.once("k3", "ep", M, M) == M()


def test_retention(be):
    now = datetime(2027, 3, 20, tzinfo=UTC)
    old, fresh = iso(now - timedelta(days=40)), iso(now - timedelta(days=1))
    for k, at in (("old", iso(now - timedelta(days=8))), ("new", fresh)):
        be.db.ex("INSERT INTO idempotency_keys(key, endpoint, response_json, created_at) VALUES(?,?,?,?)", (k, "e", "{}", at))
    for at in (old, fresh):
        be.db.ex("INSERT INTO login_attempts(ip, username, ok, at) VALUES('1.1.1.1','x',0,?)", (at,))
    done = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at) VALUES('wakeup','planned','claude-subscription','sonnet/low',5,'done',?)", (old,))
    live = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at) VALUES('wakeup','planned','claude-subscription','sonnet/low',5,'running',?)", (old,))
    for rid in (done, live):
        be.db.ex("INSERT INTO run_events(run_id, at, raw_json) VALUES(?,?,'{}')", (rid, old))
        be.db.ex("INSERT INTO timeline_items(run_id, at, kind, text) VALUES(?,?,'note','hello')", (rid, old))
    out = run_retention(be.db, be.settings, now)
    assert out["idempotency_keys"] == 1 and out["login_attempts"] == 1 and out["run_events"] == 1
    assert [r["run_id"] for r in be.db.q("SELECT run_id FROM run_events")] == [live]
    assert be.db.q1("SELECT COUNT(*) AS n FROM timeline_items")["n"] == 2   # the normalised copy stays
    assert run_retention(be.db, be.settings, now) == {}   # once a day
    be.settings.set("RUN_EVENTS_KEEP_DAYS", 100)
    assert be.settings.get("RUN_EVENTS_KEEP_DAYS") == 100


# ── DST ────────────────────────────────────────────────────────────────────
def test_hours_and_next_in_hours_across_dst():
    night = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "08:00"}]
    spring = datetime(2027, 3, 14, 1, 0, tzinfo=NY)   # 02:00-03:00 doesn't exist
    t = next_in_hours([{"days": [6], "start": "02:30", "end": "04:00"}], spring)
    assert t == datetime(2027, 3, 14, 3, 0, tzinfo=NY) and t.utcoffset() == timedelta(hours=-4)
    # Overnight window across both changes.
    for day in (datetime(2027, 3, 14, 7, 30, tzinfo=UTC).astimezone(NY), datetime(2027, 11, 7, 6, 30, tzinfo=UTC).astimezone(NY)):
        assert in_hours(night, day)
    # Fall back: 01:30 happens twice, and a window of 01:00-02:00 matches both passes.
    first = datetime(2027, 11, 7, 5, 30, tzinfo=UTC).astimezone(NY)
    second = datetime(2027, 11, 7, 6, 30, tzinfo=UTC).astimezone(NY)
    assert first.hour == second.hour == 1 and first.utcoffset() != second.utcoffset()
    win = [{"days": [6], "start": "01:00", "end": "02:00"}]
    assert in_hours(win, first) and in_hours(win, second)
    assert u(next_in_hours(win, datetime(2027, 11, 7, 5, 50, tzinfo=UTC).astimezone(NY))) == datetime(2027, 11, 7, 6, 0, tzinfo=UTC)
    assert u(next_in_hours(win, datetime(2027, 11, 7, 6, 20, tzinfo=UTC).astimezone(NY))) == datetime(2027, 11, 7, 6, 30, tzinfo=UTC)


def ny_be(be):
    be.db.ex("UPDATE users SET timezone='America/New_York' WHERE id=1")


def test_the_fallback_is_at_3am_local_on_dst_nights(c, be):
    ny_be(be)
    for now, want in ((datetime(2027, 3, 13, 12, tzinfo=UTC), datetime(2027, 3, 14, 3, tzinfo=NY)),     # spring-forward night
                      (datetime(2027, 11, 6, 16, tzinfo=UTC), datetime(2027, 11, 7, 3, tzinfo=NY))):   # fall-back night
        be.db.ex("DELETE FROM wakeups")
        be.clock.t = now
        usage(be, weekly=10, resets_in_h=100)
        tick(be)
        fb = be.db.q1("SELECT at FROM wakeups WHERE kind='fallback' AND status='planned'")
        assert fb and datetime.fromisoformat(fb["at"].replace("Z", "+00:00")) == want, fb


def test_routine_times_across_dst(be):
    ny_be(be)
    nxt = be.scheduler.next_occurrence
    # 02:30 daily on the spring-forward night lands after the gap, once.
    t = nxt({"kind": "daily", "time": "02:30"}, "claude-subscription", datetime(2027, 3, 14, 5, tzinfo=UTC))
    assert u(t) == datetime(2027, 3, 14, 7, 30, tzinfo=UTC)   # 03:30 EDT
    assert u(nxt({"kind": "daily", "time": "02:30"}, "claude-subscription", t)) == datetime(2027, 3, 15, 6, 30, tzinfo=UTC)
    # 01:30 on the fall-back night fires on the first pass only.
    t = nxt({"kind": "daily", "time": "01:30"}, "claude-subscription", datetime(2027, 11, 7, 4, tzinfo=UTC))
    assert u(t) == datetime(2027, 11, 7, 5, 30, tzinfo=UTC)
    assert u(nxt({"kind": "daily", "time": "01:30"}, "claude-subscription", t)) == datetime(2027, 11, 8, 6, 30, tzinfo=UTC)
    # Weekly: Sunday 09:00 is 13:00 UTC before the change and 14:00 after.
    assert u(nxt({"kind": "weekly", "day": 6, "time": "09:00"}, "claude-subscription", datetime(2027, 11, 6, 0, tzinfo=UTC))) == datetime(2027, 11, 7, 14, tzinfo=UTC)


def test_monthly_periods_across_dst(be):
    ny_be(be)
    be.budgets.add_api_provider("sk-go-test-0123456789abcdefghij", None, "opencode-go")
    be.clock.t = datetime(2027, 3, 20, tzinfo=UTC)
    start, end = be.budgets.week("opencode-go")
    assert start == datetime(2027, 3, 1, 5, tzinfo=UTC) and end == datetime(2027, 4, 1, 4, tzinfo=UTC)   # midnight local each time
    assert (end - start) == timedelta(days=31) - timedelta(hours=1)   # the month is an hour short
    # A before_reset routine uses the real reset instant.
    t = be.scheduler.next_occurrence({"kind": "before_reset", "hours": 6}, "opencode-go", be.clock.t)
    assert t == datetime(2027, 3, 31, 22, tzinfo=UTC)
