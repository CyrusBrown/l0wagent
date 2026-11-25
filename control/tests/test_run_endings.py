"""How runs end, in plain sentences: the mapping, a stop that interrupts the session (not an
error), and a genuine error (still one)."""

import asyncio
import json

import pytest

from l0w_control.runs.endings import ending, explain_error, stopped_sentence
from l0w_control.runs.manager import Session

from .test_run_brief import add_run
from .test_run_brief import be as _brief_be

be = _brief_be  # the same fixture, as this module's own

USAGE = "5-hour usage is at 70%, at or above your reserve (70%)."


@pytest.mark.parametrize("reason,end_reason,expected", [
    (USAGE, "usage", "Stopped: 5-hour usage reached your reserve (70%)."),
    ("Weekly usage is at 91%, at or above your reserve (90%).", "usage", "Stopped: weekly usage reached your reserve (90%)."),
    ("usage reserve reached", "usage", "Stopped: usage reached your reserve."),
    ("budget reached (10.0 of 10 pts)", "budget", "Stopped: its budget was used up."),
    ("deadline reached", "deadline", "Stopped: it reached its deadline."),
    ("stopped by you", "stopped", "Stopped by you."),
    ("you paused everything", "stopped", "Stopped: you paused everything."),
    ("the server is draining for an update", "stopped", "Stopped: the server is updating."),
    (None, None, "Stopped by you."),
    ("something odd", "stopped", "Stopped: something odd."),
])
def test_stopped_sentences(reason, end_reason, expected):
    assert stopped_sentence(reason, end_reason) == expected


@pytest.mark.parametrize("text,subtype,rc,stderr,expected", [
    ("API Error: 401 {\"type\":\"authentication_error\"}", "success", None, None, "Claude's login expired: run ./l0w claude-login on the server."),
    ("OAuth token has expired. Please run /login", None, None, None, "Claude's login expired: run ./l0w claude-login on the server."),
    ("Invalid API key", None, None, None, "Claude's login expired: run ./l0w claude-login on the server."),
    ("", "error_during_execution", None, "Please run /login", "Claude's login expired: run ./l0w claude-login on the server."),
    ("API Error: 529 overloaded_error", None, None, None, "Claude was overloaded or rate-limited; it'll retry next run."),
    ("429 rate limit", None, None, None, "Claude was overloaded or rate-limited; it'll retry next run."),
    ("error_max_turns", "error_max_turns", None, None, "The session hit its turn limit before it finished."),
    ("error_max_budget_usd", "error_max_budget_usd", None, None, "The session hit its spending limit before it finished."),
    ("error_during_execution", "error_during_execution", None, None, "The session stopped unexpectedly."),
    ("", "error_during_execution", None, "boom\nsegfault in x", "The session stopped unexpectedly: segfault in x"),
    ("", None, 137, "killed", "The runner crashed (exit 137). Last words: killed"),
    ("A plain message", None, None, None, "A plain message"),
])
def test_error_sentences(text, subtype, rc, stderr, expected):
    assert explain_error(text, subtype, rc, stderr) == expected


def test_endings():
    assert ending("running") is None
    assert ending("done", "idle") == "Finished: nothing more to do."
    assert ending("done", "finished") == "Finished."
    assert ending("stopped", "usage", USAGE) == "Stopped: 5-hour usage reached your reserve (70%)."
    assert ending("failed", "error", None, "error_during_execution") == "The session stopped unexpectedly."
    assert ending("failed", "error", None, None).startswith("The run ended with an error")


def result_event(**kw):
    return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "", "session_id": "s1", "num_turns": 2, **kw}


def feed(be, run_id, *events):
    sid = be.db.ex("INSERT INTO run_sessions(run_id, started_at, runner_session, runtime, session_id, log_path) VALUES(?,?,?,?,?,?)",
                   (run_id, be.db.now(), "r1", "claude-code", "", "x"))
    sess = Session(sid, "r1", 0, "")
    for ev in events:
        be.runs_mgr._process(run_id, sess, ev, 10)
    return sess


def items(be, run_id):
    return be.db.q("SELECT kind, text, data_json FROM timeline_items WHERE run_id=? ORDER BY id", (run_id,))


def finish(be, run_id, ok=False):
    async def commit(*a, **k):
        return {"status": "ok", "files": []}

    async def release(*a, **k):
        return None
    be.runs_mgr.runner.commit, be.runs_mgr.egress.release = commit, release
    asyncio.run(be.runs_mgr._finish(run_id, "exited", {"ok": ok, "text": ""}))
    return be.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))


def test_a_run_stopped_by_the_usage_gate_is_not_an_error(be):
    rid = add_run(be, status="running", stop_reason=USAGE, stop_requested_at="2026-01-01T00:00:00Z", result="earlier text")
    feed(be, rid, result_event(), {"type": "runner", "event": "exited", "returncode": 1, "stderr_tail": "interrupted"})
    rows = items(be, rid)
    assert not [r for r in rows if r["kind"] == "error"]
    ctl = [r for r in rows if r["kind"] == "control"]
    assert ctl[0]["text"] == "Stopped: 5-hour usage reached your reserve (70%)."
    assert json.loads(ctl[0]["data_json"])["was"].startswith("Session ended with an error")
    assert be.db.q1("SELECT result FROM runs WHERE id=?", (rid,))["result"] != "error_during_execution"
    done = finish(be, rid)
    assert done["status"] == "stopped" and done["end_reason"] == "usage"
    assert be.runs_mgr.run(rid).ending == "Stopped: 5-hour usage reached your reserve (70%)."
    assert any("Run stopped: 5-hour usage reached your reserve (70%)" in r["text"] for r in items(be, rid))
    assert not be.db.q("SELECT 1 FROM notifications WHERE kind='run_failed'")


def test_an_unstopped_error_during_execution_is_still_an_error(be):
    rid = add_run(be, status="running")
    feed(be, rid, result_event(), {"type": "runner", "event": "exited", "returncode": 1, "stderr_tail": "line one\nit broke"})
    errs = [r for r in items(be, rid) if r["kind"] == "error"]
    assert errs[0]["text"] == "Session ended with an error: The session stopped unexpectedly."
    assert json.loads(errs[0]["data_json"])["raw"] == "error_during_execution"
    assert errs[1]["text"] == "The runner crashed (exit 1). Last words: it broke"
    assert be.db.q1("SELECT result FROM runs WHERE id=?", (rid,))["result"] == "The session stopped unexpectedly."
    done = finish(be, rid)
    assert done["status"] == "failed"
    assert be.runs_mgr.run(rid).ending == "The session stopped unexpectedly."
    note = be.db.q1("SELECT title, body FROM notifications WHERE kind='run_failed'")
    assert note and note["body"] == "The session stopped unexpectedly."


def test_a_login_error_names_the_fix(be):
    rid = add_run(be, status="running")
    feed(be, rid, result_event(subtype="success", result="OAuth token has expired. Please run /login"))
    assert be.db.q1("SELECT result FROM runs WHERE id=?", (rid,))["result"] == "Claude's login expired: run ./l0w claude-login on the server."


def test_the_next_brief_uses_the_readable_ending(be):
    old = add_run(be, status="stopped", stop_reason=USAGE, end_reason="usage", used=3)
    new = add_run(be, status="running", ended=False)
    assert "was stopped (5-hour usage reached your reserve (70%))" in be.runs_mgr._since_last_run(new)
    assert old


def test_the_next_brief_carries_the_reviewers_last_word(be):
    """Run 38, 2026-11-25: "do X, then you can end. AGREE" ended the run and X never reached the agent."""
    old = add_run(be, status="done", end_reason="idle", used=3)
    be.db.ex("INSERT INTO reviewer_messages(run_id, sender, kind, body, created_at) VALUES(?,?,?,?,?)",
             (old, "reviewer", "reply", "Check the K3 output format first.\n\nAGREE", be.db.now()))
    new = add_run(be, status="running", ended=False)
    assert "The reviewer's last word: Check the K3 output format first. AGREE" in be.runs_mgr._since_last_run(new)
