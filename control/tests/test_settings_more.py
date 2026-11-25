"""Hard-coded limits that became owner settings (Settings → Advanced): they're declared, and they take effect."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.backends.base import BadRequest
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import ADVANCED, SPECS, Env

PW = "correct horse battery"

NEW = {  # key: (default, lo, hi, group, label, unit)
    "REVIEWER_GATE_MAX_ROUNDS": (3, 0, 10, "Reviewer", "Times the reviewer can send it back (Gate)", None),
    "FALLBACK_HOUR": (3, 0, 23, "Wakeups", "Fallback wakeup hour", "h"),
    "PLAN_HORIZON_DAYS": (8, 1, 31, "Wakeups", "How far ahead it may plan", "days"),
    "MAX_OPEN_ASKS_PER_RUN": (20, 1, 200, "Conversation", "Open asks one run may file", None),
    "MAX_ASK_NOTIFICATIONS_PER_HOUR": (10, 1, 200, "Notifications", "Ask notifications per hour", None),
    "UPLOAD_MAX_MB": (200, 1, 4096, "Files", "Largest upload", "MB"),
    "SHELF_SNAPSHOT_MAX_MB": (500, 1, 4096, "Files", "Largest shelf snapshot", "MB"),
    "CHAT_MAX_STEPS": (12, 1, 50, "Chat", "Tool steps per chat reply", None),
    "LOST_CONTACT_GIVE_UP_MINUTES": (10, 2, 120, "Runs", "Give up on a silent agent computer after", "min"),
    "LOGIN_HISTORY_DAYS": (30, 1, 3650, "System", "Keep sign-in history for", "days"),
}


@pytest.fixture
def be(tmp_path, monkeypatch):
    monkeypatch.setenv("L0W_SCHEDULER", "0")
    monkeypatch.setenv("L0W_INBOX_DIR", str(tmp_path / "work" / "_inbox"))
    (tmp_path / "work" / "_inbox").mkdir(parents=True)
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    return SqliteBackend(env=env)


@pytest.fixture
def c(be):
    with TestClient(create_app(be), client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        yield c


def test_new_settings_are_declared():
    for key, (default, lo, hi, group, label, unit) in NEW.items():
        sp = SPECS[key]
        assert (sp.kind, sp.default, sp.lo, sp.hi, sp.who) == ("int", default, lo, hi, "owner"), key
        assert sp.desc.endswith("."), key
        assert ADVANCED[key] == (group, label, unit), key
    groups = [g for g, _, _ in ADVANCED.values()]
    seen: list[str] = []
    for g in groups:
        if not seen or seen[-1] != g:
            seen.append(g)
    assert len(seen) == len(set(seen))  # each group's items are together
    assert seen == ["Conversation", "Runs", "Pacing (Max)", "Wakeups", "Reviewer", "Chat", "Files", "Notifications", "Friends", "System"]


def test_ask_cap_follows_the_setting(be):
    be.db.ex("INSERT INTO runs(id, kind, status, why, started_at) VALUES(?,?,?,?,?)", (901, "wakeup", "running", "manual", be.db.now()))
    be.settings.set("MAX_OPEN_ASKS_PER_RUN", 2)
    for i in range(2):
        be.conv.file_task(901, None, f"task {i}", None)
    with pytest.raises(BadRequest) as e:
        be.conv.file_task(901, None, "too many", None)
    assert e.value.error == "too_many_asks"
    be.settings.set("MAX_OPEN_ASKS_PER_RUN", 3)
    be.conv.file_task(901, None, "now fits", None)


def test_plan_horizon_follows_the_setting(be):
    be.setup_owner(__import__("l0w_control.api.schemas", fromlist=["x"]).SetupOwnerRequest(username="mara", password=PW, timezone="UTC"))
    now = be.budgets.now()
    at = now + timedelta(days=5)
    be.settings.set("PLAN_HORIZON_DAYS", 3)
    assert be.budgets.validate("claude-subscription", at, 1, "sonnet", by="owner") == "Plan at most 3 days ahead."
    be.settings.set("PLAN_HORIZON_DAYS", 8)
    assert be.budgets.validate("claude-subscription", at, 1, "sonnet", by="owner") != "Plan at most 8 days ahead."


def test_upload_limit_follows_the_setting(c, be):
    be.settings.set("UPLOAD_MAX_MB", 1)
    big = b"x" * (1024 * 1024 + 1)
    r = c.post("/api/uploads", files={"file": ("big.bin", big)})
    assert r.status_code == 400 and "limited to 1 MB" in r.json()["detail"]
    r = c.post("/api/uploads", files={"file": ("small.bin", b"x" * 1000)})
    assert r.status_code == 201


def test_gate_rounds_follow_the_setting(be):
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, reviewer_mode, bucket, gate_rounds) "
                   "VALUES('wakeup','planned','claude-subscription','sonnet/medium',20,'running',?,'gate','allowance',1)", (be.db.now(),))
    asked = []

    async def approve(run_id, intro, text):
        asked.append(run_id)
        return True, ""

    be.reviewer.approve = approve
    sess = SimpleNamespace(runtime_session="s1")
    be.settings.set("REVIEWER_GATE_MAX_ROUNDS", 1)
    assert asyncio.run(be.runs_mgr._gate(rid, sess, {"text": "IDLE: done"})) == "done"
    assert asked == []  # already sent back once: the reviewer isn't asked again
    be.settings.set("REVIEWER_GATE_MAX_ROUNDS", 3)
    assert asyncio.run(be.runs_mgr._gate(rid, sess, {"text": "IDLE: done"})) == "agreed"
    assert asked == [rid]


def test_a_silent_gate_falls_back_to_the_carry_on_nudge(be):
    """2026-11-25: a reviewer out of its share (or quiet, or failing) used to count as agreeing, ending runs with budget left."""
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, deadline, reviewer_mode, bucket) "
                   "VALUES('wakeup','planned','claude-subscription','sonnet/medium',20,'running',?,?,'gate','allowance')",
                   (be.db.now(), "2099-01-01T00:00:00Z"))

    async def approve(run_id, intro, text):
        return None, ""

    started = []

    async def start_session(run_id, resume=None, reason=None, prompt=None):
        started.append(prompt)

    be.reviewer.approve = approve
    be.runs_mgr._start_session = start_session
    sess = SimpleNamespace(runtime_session="s1", id=0)
    assert asyncio.run(be.runs_mgr._gate(rid, sess, {"text": "IDLE: done"})) == "silent"
    assert asyncio.run(be.runs_mgr._nudge(rid, sess, {"text": "IDLE: done"})) is True  # gate mode no longer skips the nudge
    assert started and "use it or lose it" in started[0]


def test_second_batch_of_settings_is_declared():
    from l0w_control.config import ADVANCED, SPECS

    for key, default in {"FALLBACK_NIGHTS": 2, "BRIEF_LIST_CAP": 15, "NEWS_CAP": 6, "CHAT_MAX_TOKENS": 32000, "INTEGRATION_TIMEOUT_S": 60,
                         "ZIP_MAX_MB": 1024, "ZIP_MAX_FILES": 50000}.items():
        assert SPECS[key].default == default and key in ADVANCED


def test_the_hook_reminds_it_to_leave_run_notes(be, monkeypatch):
    """Owner, 2026-11-25: the old assistant's reminder after 5 minutes without a run note, as a setting (0 = off)."""
    from datetime import UTC, datetime

    from l0w_control.api import agent as A
    from l0w_control.auth import iso

    t0 = datetime(2027, 3, 1, 12, tzinfo=UTC)
    now = {"t": t0}
    monkeypatch.setattr(A, "utcnow", lambda: now["t"])
    rid = be.db.ex("INSERT INTO runs(kind, why, provider, tier, budget, status, started_at, bucket) "
                   "VALUES('wakeup','planned','claude-subscription','sonnet/medium',20,'running',?,'allowance')", (iso(t0),))
    assert be.settings.get("NOTE_REMINDER_MINUTES") == 5 and ADVANCED["NOTE_REMINDER_MINUTES"][0] == "Runs"
    now["t"] = t0 + timedelta(minutes=1)
    assert A._note_reminder(be, rid) is None
    now["t"] = t0 + timedelta(minutes=3)
    assert "haven't left a run note yet" in A._note_reminder(be, rid)   # the first one after 2 minutes
    be.db.ex("INSERT INTO run_notes(run_id, at, text) VALUES(?,?,?)", (rid, iso(now["t"]), "Writing the export"))
    now["t"] += timedelta(minutes=4)
    assert A._note_reminder(be, rid) is None
    now["t"] += timedelta(minutes=2)
    assert "6 min ago" in A._note_reminder(be, rid)
    now["t"] += timedelta(minutes=1)
    assert A._note_reminder(be, rid) is None                              # at most once per interval
    be.settings.set("NOTE_REMINDER_MINUTES", 0)
    now["t"] += timedelta(minutes=30)
    assert A._note_reminder(be, rid) is None                              # off
    be.settings.set("NOTE_REMINDER_MINUTES", 5)
    be.db.ex("UPDATE runs SET kind='reply' WHERE id=?", (rid,))
    assert A._note_reminder(be, rid) is None                              # not in Reply-now runs
