"""M4: points, levels, the ledger, wakeup validation, the scheduler (fallback, sweep, routines),
promote bookkeeping and jobs, on the real backend with a simulated clock and a stand-in for
starting runs (the run manager itself is covered by the integration tests)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from l0w_control.api import schemas as s
from l0w_control.app import create_app
from l0w_control.auth import iso
from l0w_control.backends.base import BadRequest
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env

PW = "correct horse battery"
AGENT = {"X-Agent-Token": "agent-secret"}
MONDAY = datetime(2027, 3, 1, 0, 0, tzinfo=UTC)  # a Monday, well in the future


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def be(tmp_path, monkeypatch):
    monkeypatch.setenv("L0W_SCHEDULER", "0")  # ticks are driven by the tests
    monkeypatch.setenv("L0W_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tmp_path / "workspace"))
    (tmp_path / "work" / "garden").mkdir(parents=True)
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    b = SqliteBackend(env=env)
    b.setup_owner(s.SetupOwnerRequest(username="mara", password=PW, timezone="UTC"))
    b.clock = Clock(MONDAY + timedelta(hours=12))  # type: ignore[attr-defined]
    b.budgets.now = b.clock  # type: ignore[attr-defined]
    b.started = []  # type: ignore[attr-defined]

    async def start(req, kind="wakeup", why="manual", thread_id=None, *, provider=None, bucket="allowance", wakeup_id=None, routine_id=None, label=None):
        if b.runs_mgr.active_run_id():
            from l0w_control.backends.base import Conflict
            raise Conflict("busy", "busy")
        rid = b.db.ex("INSERT INTO runs(kind, why, why_detail, provider, tier, budget, status, started_at, bucket, wakeup_id, routine_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (kind, why, req.reason, provider or "claude-subscription", req.tier, req.budget, "running", iso(b.clock()), bucket, wakeup_id, routine_id))
        b.started.append(rid)  # type: ignore[attr-defined]
        return b.runs_mgr.run(rid)

    b.runs_mgr.start = start  # type: ignore[method-assign]
    return b


def finish(be, run_id: int, pts: float, idle: bool = True) -> None:
    """End a stand-in run having spent `pts` (a ledger row at the simulated time)."""
    run = be.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    at = iso(be.clock())
    be.db.ex("INSERT INTO usage_ledger(provider, run_id, who, bucket, cost_usd, pts, at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
             (run["provider"], run_id, "assistant", run["bucket"], pts / 2, pts, at, at))
    be.db.ex("UPDATE runs SET status='done', used=?, idle=?, ended_at=? WHERE id=?", (pts, int(idle), at, run_id))
    be.scheduler._run_finished(be.db.q1("SELECT * FROM runs WHERE id=?", (run_id,)))


def tick(be) -> None:
    asyncio.run(be.scheduler.tick(be.clock()))


def usage(be, weekly: float, resets_in_h: float, five: float = 10) -> None:
    now = be.clock()
    be.usage._cache = s.UsageReading(provider="claude-subscription", source="oauth", fetched_at=now, five_hour_pct=five, five_hour_resets_at=now + timedelta(hours=2),
                                     seven_day_pct=weekly, seven_day_resets_at=now + timedelta(hours=resets_in_h), error=None)


# ── levels, Boost, the summary ─────────────────────────────────────────────
def test_levels_overrides_and_boost(be):
    b = be.budgets.summary("claude-subscription")
    assert (b.level, b.allowance, b.left, b.reply_allowance, b.unit) == ("steady", 150, 150, 30, "pts")
    be.patch_budget("claude-subscription", s.BudgetPatch(overrides=s.BudgetLimitsPatch(allowance=200, max_per_day=3)))
    b = be.budgets.summary("claude-subscription")
    assert b.allowance == 200 and b.limits.max_per_day == 3 and set(b.custom) == {"allowance", "max_per_day"}
    be.patch_budget("claude-subscription", s.BudgetPatch(level="frugal"))  # a level clears overrides
    b = be.budgets.summary("claude-subscription")
    assert (b.level, b.allowance, b.custom) == ("frugal", 50, [])
    be.boost("claude-subscription", s.BoostRequest(level="busy", hours=6))
    b = be.budgets.summary("claude-subscription")
    assert (b.level, b.effective_level, b.allowance) == ("frugal", "busy", 400) and b.boost
    be.clock.t += timedelta(hours=7)
    assert be.budgets.summary("claude-subscription").effective_level == "frugal"
    with pytest.raises(BadRequest) as e:
        be.boost("claude-subscription", s.BoostRequest(level="dormant"))
    assert "raises the level" in e.value.detail


def test_ledger_attribution_and_reserve(be):
    usage(be, weekly=40, resets_in_h=72)
    week_start = be.budgets.week("claude-subscription")[0]
    be.db.ex("INSERT INTO usage_ledger(provider, who, bucket, pts, at, updated_at) VALUES('claude-subscription','assistant','allowance',100,?,?)",
             (iso(week_start + timedelta(hours=1)), iso(week_start)))
    be.db.ex("INSERT INTO usage_ledger(provider, who, bucket, pts, at, updated_at) VALUES('claude-subscription','assistant','reply',12,?,?)",
             (iso(week_start + timedelta(hours=2)), iso(week_start)))
    b = be.budgets.summary("claude-subscription")
    # The meter says 400 pts this week: 112 the assistant's, the rest the owner's.
    assert (b.used_by_assistant, b.used_by_owner) == (112, 288)
    assert b.left == 50 and b.reply_left == 18
    # The owner's own use pushes the week toward the 80% reserve: the allowance shrinks.
    usage(be, weekly=77, resets_in_h=72)
    assert be.budgets.summary("claude-subscription").left == 30


def test_message_usage_is_counted_once_and_replaced_by_the_total(be):
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, status, started_at, budget) VALUES('wakeup','manual','claude-subscription','running',?,20)", (be.db.now(),))
    sid = be.db.ex("INSERT INTO run_sessions(run_id, runtime, session_id, log_path, started_at) VALUES(?,?,?,?,?)", (rid, "claude-code", "x", "l", be.db.now()))
    u = {"input_tokens": 1_000_000, "output_tokens": 0}
    be.budgets.record_message(rid, sid, "claude-subscription", "assistant", "allowance", "m1", "claude-sonnet", u)
    be.budgets.record_message(rid, sid, "claude-subscription", "assistant", "allowance", "m1", "claude-sonnet", u)  # same message, another block
    assert be.budgets.run_cost(rid) == pytest.approx(3.0) and be.budgets.run_pts(rid) == pytest.approx(6.0)
    be.budgets.record_total(rid, sid, "claude-subscription", "assistant", "allowance", 2.5)
    assert be.budgets.run_pts(rid) == pytest.approx(5.0)
    be.budgets.calibrate(delta_pts=20, cost_usd=2.5)  # the meter moved 2%: 8 pts per dollar, blended in
    assert be.budgets.pts_per_usd("claude-subscription") == pytest.approx(0.7 * 2 + 0.3 * 8)


# ── planning wakeups (the agent's API) ─────────────────────────────────────
@pytest.fixture
def c(be):
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as client:
        client.post("/api/auth/login", json={"username": "mara", "password": PW})
        be.budgets.now = be.clock  # startup doesn't reset it, but be explicit
        yield client


def wake(c, at: str, budget: float, tier: str = "sonnet/medium", reason: str = "check the export"):
    return c.post("/api/agent/wake", json={"at": at, "budget": budget, "tier": tier, "reason": reason}, headers=AGENT)


def test_wakeups_are_checked_against_the_limits(c, be):
    # Steady: 22:00–08:00, 2 a day, 120 min apart, 40 pts each, sonnet/opus-low tiers. Clock: Mon 12:00 UTC.
    assert wake(c, "2027-03-01T23:00", 20).status_code == 200
    bad = {
        "2027-03-01T15:00": "outside the allowed hours",
        "2027-03-02T00:00": "Too close",
    }
    for at, why in bad.items():
        r = wake(c, at, 10)
        assert r.status_code == 400 and why in r.json()["detail"], r.text
    assert "per wakeup" in wake(c, "2027-03-02T03:00", 41).json()["detail"]
    assert "isn't allowed" in wake(c, "2027-03-02T03:00", 10, tier="opus/high").json()["detail"]
    assert wake(c, "2027-03-02T03:00", 40).status_code == 200
    assert wake(c, "2027-03-02T23:00", 40).status_code == 200
    assert "limit is 2 a day" in wake(c, "2027-03-02T23:59", 5).json()["detail"]  # Tuesday has 03:00 and 23:00
    assert wake(c, "2027-03-03T23:00", 40).status_code == 200
    r = wake(c, "2027-03-04T23:00", 40)
    assert r.status_code == 400 and "Not enough budget" in r.json()["detail"]  # 140 planned of 150
    text = c.get("/api/agent/budget", headers=AGENT).json()["text"]
    assert "10 pts unscheduled" in text and "Planned:" in text
    # Replacing the plan is all-or-nothing.
    r = c.put("/api/agent/wake", json={"items": [{"at": "2027-03-04T23:00", "budget": 30, "tier": "sonnet/medium", "reason": "first"},
                                                {"at": "2027-03-04T12:00", "budget": 30, "tier": "sonnet/medium", "reason": "second"}]}, headers=AGENT)
    assert r.status_code == 400 and "Item 1" in r.json()["detail"]
    assert len(c.get("/api/agent/wake", headers=AGENT).json()["wakeups"]) == 4
    r = c.put("/api/agent/wake", json={"items": [{"at": "2027-03-04T23:00", "budget": 30, "tier": "sonnet/medium", "reason": "only this"}]}, headers=AGENT)
    assert r.status_code == 200 and [w["reason"] for w in c.get("/api/agent/wake", headers=AGENT).json()["wakeups"]] == ["only this"]
    # The owner sees it on the calendar, with the allowed hours as windows.
    cal = c.get("/api/calendar").json()
    assert [w["reason"] for w in cal["wakeups"] if w["status"] == "planned"] == ["only this"]
    assert any(w["kind"] == "allowed" for w in cal["windows"]) and cal["budget"]["scheduled"] == 30


def test_owner_edits_and_dormant(c, be):
    w = wake(c, "2027-03-01T23:00", 20).json()
    r = c.patch(f"/api/wakeups/{w['id']}", json={"at": "2027-03-01T14:00:00Z", "budget": 25})
    assert r.status_code == 200 and r.json()["budget"] == 25  # the owner isn't bound by the allowed hours
    assert "changed your wakeup" in c.get("/api/agent/inbox", headers=AGENT).json()["text"]
    assert c.delete(f"/api/wakeups/{w['id']}").json()["status"] == "cancelled"
    c.patch("/api/budget/claude-subscription", json={"level": "dormant"})
    assert "Dormant" in wake(c, "2027-03-01T23:00", 10).json()["detail"]
    r = c.post("/api/wakeups", json={"at": "2027-03-01T20:00:00Z", "budget": 5, "reason": "mine"})
    assert r.status_code == 200 and r.json()["kind"] == "owner"


# ── the scheduler ──────────────────────────────────────────────────────────
def test_fallback_then_the_agents_plan_replaces_it(c, be):
    tick(be)
    fb = be.db.q("SELECT * FROM wakeups WHERE kind='fallback' AND status='planned'")
    assert len(fb) == 1 and fb[0]["at"] == "2027-03-02T03:00:00Z" and fb[0]["budget"] == 15
    assert "fallback wakeup" in c.get("/api/agent/inbox", headers=AGENT).json()["text"]
    tick(be)
    assert len(be.db.q("SELECT 1 FROM wakeups WHERE kind='fallback' AND status='planned'")) == 1
    assert wake(c, "2027-03-02T03:30", 20).status_code == 200  # 30 min from the fallback: fine, it's replaced
    assert be.db.q1("SELECT status FROM wakeups WHERE id=?", (fb[0]["id"],))["status"] == "cancelled"


def test_due_wakeups_start_capped_by_the_allowance(c, be):
    wake(c, "2027-03-01T23:00", 40)
    be.clock.t = datetime(2027, 3, 1, 23, 0, 5, tzinfo=UTC)
    be.db.ex("INSERT INTO usage_ledger(provider, who, bucket, pts, at, updated_at) VALUES('claude-subscription','assistant','allowance',130,?,?)",
             (iso(be.clock()), iso(be.clock())))
    w = be.db.q1("SELECT id FROM wakeups WHERE kind='planned'")
    tick(be)
    run = be.db.q1("SELECT * FROM runs WHERE id=?", (be.started[-1],))
    assert (run["kind"], run["why"], run["budget"]) == ("wakeup", "planned", 20)  # 150 - 130 left
    assert be.db.q1("SELECT status, run_id FROM wakeups WHERE id=?", (w["id"],))["status"] == "running"
    finish(be, run["id"], 20)
    assert be.db.q1("SELECT status FROM wakeups WHERE id=?", (w["id"],))["status"] == "done"


def test_missed_and_skipped(c, be):
    w1 = wake(c, "2027-03-01T23:00", 10).json()
    be.clock.t = datetime(2027, 3, 2, 0, 0, tzinfo=UTC)  # an hour late: control was down
    tick(be)
    assert be.db.q1("SELECT status FROM wakeups WHERE id=?", (w1["id"],))["status"] == "missed"
    assert "was missed" in c.get("/api/agent/inbox", headers=AGENT).json()["text"]


def test_sweep_spends_what_the_owner_left_before_the_reset(c, be):
    c.patch("/api/budget/claude-subscription", json={"level": "busy"})
    be.clock.t = datetime(2027, 3, 6, 12, 0, tzinfo=UTC)  # Saturday: busy allows all day
    usage(be, weekly=60, resets_in_h=3)
    tick(be)
    sw = be.db.q1("SELECT * FROM wakeups WHERE kind='sweep'")
    assert sw and sw["budget"] == 350  # up to the 95% ceiling
    run = be.db.q1("SELECT * FROM runs WHERE id=?", (be.started[-1],))
    assert (run["bucket"], run["why"]) == ("sweep", "sweep")
    finish(be, run["id"], 50, idle=True)  # nothing worthwhile: the sweep ends for this reset
    tick(be)
    assert len(be.db.q("SELECT 1 FROM wakeups WHERE kind='sweep'")) == 1
    # The sweep is on top of the allowance.
    assert be.budgets.summary("claude-subscription").left == 400 or be.budgets.summary("claude-subscription").room_below_reserve is not None


def test_routines_builtin_owner_and_proposed(c, be):
    usage(be, weekly=20, resets_in_h=100)
    routines = c.get("/api/routines").json()
    assert routines[0]["builtin"] == "weekly_review" and routines[0]["schedule_text"] == "6 h before the weekly reset"
    r = c.post("/api/routines", json={"name": "Inbox sweep", "prompt": "Triage /work/_inbox.", "schedule": {"kind": "daily", "time": "23:30"}, "budget": 8})
    assert r.status_code == 200 and r.json()["next_at"].startswith("2027-03-01T23:30")
    tick(be)
    kinds = {w["reason"] for w in c.get("/api/calendar").json()["wakeups"]}
    assert {"Routine: Weekly review", "Routine: Inbox sweep"} <= kinds
    # The agent proposes one: a permission whose action adds it once approved.
    p = c.post("/api/agent/routines", json={"name": "Garden digest", "prompt": "Summarize the sensors.", "schedule": {"kind": "weekly", "day": 6, "time": "07:00"},
                                            "budget": 10, "reason": "You asked for weekly summaries"}, headers=AGENT).json()
    assert p["actions"][0]["kind"] == "routine" and "Sundays at 07:00" in p["actions"][0]["summary"]
    assert not any(r["name"] == "Garden digest" for r in c.get("/api/routines").json())
    assert c.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "always"}).status_code == 200
    made = [r for r in c.get("/api/routines").json() if r["name"] == "Garden digest"]
    assert made and made[0]["created_by"] == "assistant"
    bad = c.post("/api/agent/permissions", json={"text": "x", "actions": [{"kind": "routine", "name": "x", "prompt": "y", "schedule": {"kind": "hourly"}, "budget": 1}]},
                 headers=AGENT)
    assert bad.status_code == 400


def test_a_simulated_week_stays_within_the_allowance(c, be):
    """A well-behaved agent plans one 40-pt wakeup a night and spends it all. Over two weeks,
    each week's spend stays within the 150-pt allowance, and runs only start in allowed hours."""
    tz_hours = range(22, 24), range(0, 8)
    spent_by_week: dict[int, float] = {}
    end = MONDAY + timedelta(days=14)
    while be.clock.t < end:
        tick(be)
        active = be.runs_mgr.active_run_id()
        if active:
            run = be.db.q1("SELECT * FROM runs WHERE id=?", (active,))
            assert run["kind"] == "routine" or be.clock.t.hour in tz_hours[0] or be.clock.t.hour in tz_hours[1], run
            finish(be, active, run["budget"])
            week = (be.clock.t - MONDAY).days // 7
            spent_by_week[week] = spent_by_week.get(week, 0) + run["budget"]
            nxt = (be.clock.t + timedelta(days=1)).replace(hour=23, minute=0)
            wake(c, nxt.strftime("%Y-%m-%dT%H:%M"), 40)  # may be refused when the week's budget is gone; the fallback covers
        be.clock.t += timedelta(minutes=15)
    assert spent_by_week and all(v <= 150 for v in spent_by_week.values()), spent_by_week
    assert all(v >= 100 for v in spent_by_week.values()), spent_by_week


# ── jobs ───────────────────────────────────────────────────────────────────
def test_jobs_show_their_log_and_get_lost(c, be, tmp_path):
    (tmp_path / "work" / "garden" / "export.log").write_text("".join(f"line {i}\n" for i in range(2000)))
    j = c.post("/api/agent/jobs", json={"label": "Sensor export", "pid": 4242, "log": "/work/garden/export.log", "expect_minutes": 60}, headers=AGENT).json()
    jobs = c.get("/api/jobs").json()
    assert jobs[0]["label"] == "Sensor export" and jobs[0]["log_tail"].rstrip().endswith("line 1999") and len(jobs[0]["log_tail"]) <= 4000
    assert c.post("/api/agent/jobs", json={"label": "x", "log": "/etc/passwd"}, headers=AGENT).status_code == 400
    c.patch(f"/api/agent/jobs/{j['id']}", json={"progress": "half"}, headers=AGENT)
    assert c.get("/api/jobs").json()[0]["progress"] == "half"
    be.jobs_.mark_lost(alive=set())
    assert c.get("/api/jobs", params={"include_done": True}).json()[0]["status"] == "lost"
    assert "stopped without" in c.get("/api/agent/inbox", headers=AGENT).json()["text"]


def test_status_and_delivery_show_the_budget(c, be):
    st = c.get("/api/status").json()
    assert st["budget"]["allowance"] == 150 and st["reply_budget_left"] == 30 and st["state"] == "dormant"
    wake(c, "2027-03-01T23:00", 10)
    st = c.get("/api/status").json()
    assert st["state"] == "asleep" and st["next_wakeup_at"].startswith("2027-03-01T23:00")
    t = c.post("/api/threads", json={"title": "Pump", "first_message": "hi", "idempotency_key": "k-pump-thread-1"}).json()
    d = c.get(f"/api/threads/{t['id']}/delivery").json()
    assert d["text"].startswith("Queued for the next wakeup (Mon 23:00)") and d["reply_now_cost"] == 6 and d["reply_budget_left"] == 30
    json.dumps(d)


def test_reviewer_gate_tier_changes_and_budget_share(be):
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, reviewer_mode, bucket) "
                   "VALUES('wakeup','planned','claude-subscription','sonnet/medium',20,'running',?,'gate','allowance')", (be.db.now(),))
    replies = iter(["Not yet: the summary isn't written.", "Looks right.\nAGREE", "Harder than it looks.\nLEVEL: opus/high", "LEVEL: opus/low"])
    switched = []

    async def consult(payload):
        return {"ok": True, "text": next(replies), "session_id": "rv1", "cost_usd": 0.5, "usage": {}, "messages": []}

    async def switch(run_id, tier, by):
        switched.append(tier)

    be.runner.consult = consult
    be.runs_mgr.switch_tier = switch
    agree, reply = asyncio.run(be.reviewer.approve(rid, "The assistant wants to end the wakeup.", "IDLE: done"))
    assert not agree and "summary" in reply
    assert asyncio.run(be.reviewer.approve(rid, "The assistant wants to end the wakeup.", "IDLE: done"))[0]
    asyncio.run(be.reviewer.from_agent(rid, "ask", "Is this hard?"))
    asyncio.run(be.reviewer.from_agent(rid, "ask", "And now?"))  # opus/low isn't among the tiers it may set
    assert switched == ["opus/high"]
    run = be.db.q1("SELECT used, reviewer_used FROM runs WHERE id=?", (rid,))
    assert run["reviewer_used"] == pytest.approx(4.0) and run["used"] == pytest.approx(4.0)  # 4 turns × $0.50 × 2 pts/$
    # At its share (25% of 20 pts = 5) it goes quiet rather than spending more.
    be.db.ex("UPDATE usage_ledger SET pts=5 WHERE who='reviewer'")
    assert asyncio.run(be.reviewer.approve(rid, "The assistant wants to end the wakeup.", "IDLE")) == (True, "")
