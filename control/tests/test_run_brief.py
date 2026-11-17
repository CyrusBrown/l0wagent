"""The wakeup brief (what control tells the agent), IDLE detection, the nudge, the gate on a
clean end without IDLE, and the agent's read-only run commands."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from l0w_control.api import schemas as s
from l0w_control.app import create_app
from l0w_control.auth import iso
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env
from l0w_control.runs.manager import BRIEF_LIST_CAP, ends_idle

from .stack import Stack

PW = "correct horse battery"
AGENT = {"X-Agent-Token": "agent-secret"}
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("text,expected", [
    ("IDLE: nothing left", True),
    ("  IDLE: indented", True),
    ("Tidied the journal.\n\nIDLE: nothing else worth doing.\n", True),
    ("IDLE: first\nthen more", True),
    ("Done for now.", False),
    ("I could write IDLE: later\nand carry on", False),
    ("IDLE", False),
    ("", False),
    (None, False),
])
def test_ends_idle(text, expected):
    assert ends_idle(text) is expected


@pytest.fixture
def be(tmp_path, monkeypatch):
    monkeypatch.setenv("L0W_SCHEDULER", "0")
    monkeypatch.setenv("L0W_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tmp_path / "workspace"))
    (tmp_path / "work").mkdir()
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    b = SqliteBackend(env=env)
    b.setup_owner(s.SetupOwnerRequest(username="mara", password=PW, timezone="UTC"))
    return b


def add_run(be, *, status="running", ended=True, **kw) -> int:
    now = datetime.now(UTC)
    row = {"kind": "wakeup", "why": "planned", "provider": "claude-subscription", "tier": "sonnet/medium", "budget": 20, "status": status,
           "started_at": iso(now - timedelta(hours=2)), "bucket": "allowance", "deadline": iso(now + timedelta(hours=1))} | kw
    if ended and status not in ("starting", "running"):
        row.setdefault("ended_at", iso(now - timedelta(hours=1)))
    cols = ",".join(row)
    return be.db.ex(f"INSERT INTO runs({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))


def add_thread(be, title: str) -> int:
    return be.db.ex("INSERT INTO threads(title, status, created_by, created_at, updated_at) VALUES(?, 'open', 'owner', ?, ?)", (title, be.db.now(), be.db.now()))


def test_the_brief_says_how_the_last_run_ended_and_what_is_waiting(be):
    old = add_run(be, status="stopped", stop_reason="budget reached", end_reason="budget", used=20, now_sentence="Fixing the pump export", idle=0)
    for n in ("one", "two", "three", "four"):
        be.db.ex("INSERT INTO run_notes(run_id, at, text) VALUES(?,?,?)", (old, be.db.now(), f"note {n}"))
    tid = add_thread(be, "Pumps")
    be.db.ex("INSERT INTO claims(run_id, thread_id, plan, status, result, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
             (old, tid, "Fix the double trigger", "partial", "The run ended before this was closed.", be.db.now(), be.db.now()))
    be.db.ex("INSERT INTO timeline_items(run_id, at, kind, simple, text, data_json) VALUES(?,?,?,?,?,?)",
             (old, be.db.now(), "diff", 1, "Memory: 2 file(s) changed", json.dumps({"volume": "workspace", "files": [{"path": "NOW.md"}, {"path": "journal/x.md"}]})))
    be.db.ex("INSERT INTO questions(thread_id, run_id, kind, text, options_json, default_json, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
             (tid, old, "text", "Which day should I run the pump test, given that the weekend is busy and Monday is a holiday for everyone?", "[]", '"Sat"', "open",
              iso(datetime.now(UTC) - timedelta(days=3))))
    qid = be.db.q1("SELECT id FROM questions")["id"]
    new = add_run(be)
    brief, _ = be.runs_mgr._prompt(new)
    assert "## Since your last run" in brief
    assert f"Run #{old}" in brief and "was stopped (budget reached)" in brief and "used 20.0 of 20 pts" in brief
    assert "Its last status: Fixing the pump export" in brief
    assert "note two | note three | note four" in brief and "note one" not in brief
    assert "Claims it left open: thread #" in brief and "Fix the double trigger" in brief
    assert "without committing" in brief and "NOW.md, journal/x.md" in brief
    line = next(ln for ln in brief.splitlines() if ln.startswith(f"- question #{qid} in thread #{tid}"))
    assert "“Pumps”: Which day should I run the pump test" in line and line.endswith("…, asked 3 days ago")
    assert "Deadline " in brief and "UTC" not in brief.split("Deadline ")[1].split("\n")[0]


def test_no_previous_run_no_section_and_the_brief_size_is_recorded(be):
    new = add_run(be)
    brief, _ = be.runs_mgr._prompt(new)
    assert "## Since your last run" not in brief
    item = be.db.q1("SELECT data_json FROM timeline_items WHERE run_id=? AND text LIKE 'Brief:%'", (new,))
    assert json.loads(item["data_json"])["brief_chars"] == len(brief)


def test_long_lists_are_capped_with_a_pointer(be):
    tid = add_thread(be, "T")
    old = add_run(be, status="done")
    for i in range(BRIEF_LIST_CAP + 3):
        be.db.ex("INSERT INTO claims(run_id, thread_id, plan, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                 (old, tid, f"plan {i}", "planned", be.db.now(), be.db.now()))
    new = add_run(be)
    brief, _ = be.runs_mgr._prompt(new)
    assert "3 more open claims" in brief
    assert brief.count("- thread #") == BRIEF_LIST_CAP


def test_the_projects_section_is_capped(be):
    for i in range(BRIEF_LIST_CAP + 4):
        be.db.ex("INSERT INTO projects(slug, name, created_by, created_at, updated_at) VALUES(?,?,'owner',?,?)", (f"p{i:02d}", f"Project {i}", be.db.now(), be.db.now()))
    extra = be._brief_extra({"kind": "wakeup"})
    assert extra.count("- p") == BRIEF_LIST_CAP and "4 more projects: `assistant project list`" in extra


def test_the_agent_can_read_runs(be):
    old = add_run(be, status="done", used=7.5, idle=1, end_reason="idle", description="Tidied the garden notes", result="IDLE: all tidy")
    be.db.ex("INSERT INTO run_notes(run_id, at, text) VALUES(?,?,?)", (old, be.db.now(), "milestone"))
    c = TestClient(create_app(be))
    rows = c.get("/api/agent/runs?limit=5", headers=AGENT).json()
    assert rows[0]["id"] == old and rows[0]["used"] == 7.5 and rows[0]["about"] == "Tidied the garden notes" and rows[0]["end_reason"] == "idle"
    one = c.get(f"/api/agent/runs/{old}", headers=AGENT).json()
    assert one["result"] == "IDLE: all tidy" and one["notes"][0].endswith("milestone")
    assert c.get("/api/agent/runs/9999", headers=AGENT).status_code == 404
    assert c.get("/api/agent/runs").status_code in (401, 403)


def test_the_cli_help_prints_sections():
    cli = REPO / "agent" / "bin" / "assistant"
    out = subprocess.run([sys.executable, str(cli), "help", "shelf"], capture_output=True, text=True, check=True).stdout
    assert out.startswith("The shelf") and "assistant shelf add" in out and "assistant budget" not in out
    sections = subprocess.run([sys.executable, str(cli), "help"], capture_output=True, text=True, check=True).stdout
    assert "Long jobs" in sections and "Basics" in sections
    assert "assistant runs" in subprocess.run([sys.executable, str(cli), "help", "runs"], capture_output=True, text=True, check=True).stdout


def test_the_hook_skips_a_hung_control_for_a_while(tmp_path):
    hook = REPO / "agent" / "bin" / "assistant-hook"
    env = {"CONTROL_URL": "http://127.0.0.1:1", "AGENT_API_TOKEN": "x", "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run([sys.executable, str(hook)], input="{}", env=env, check=True, text=True)
    marks = list(tmp_path.glob("assistant-hook-failed-*"))
    assert len(marks) == 1
    import os
    import time
    before = marks[0].stat().st_mtime
    time.sleep(0.05)
    subprocess.run([sys.executable, str(hook)], input="{}", env=env, check=True, text=True)
    assert marks[0].stat().st_mtime == before          # skipped: no new attempt, no new failure mark
    os.utime(marks[0], (time.time() - 60, time.time() - 60))
    subprocess.run([sys.executable, str(hook)], input="{}", env=env, check=True, text=True)
    assert marks[0].stat().st_mtime > time.time() - 30   # the check ran again and failed again


@pytest.fixture
def stack(tmp_path):
    s_ = Stack(tmp_path, steps=2, delay=0.1).up()
    s_.login()
    yield s_
    s_.down()


def finished(stack: Stack, run_id: int) -> dict:
    return stack.wait_for(lambda: (r := stack.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="run to end")


def test_a_wrap_up_before_idle_still_counts_as_idle(stack: Stack):
    c = stack.client
    assert c.put("/api/instructions", json={"text": "fake:wrapup"}).status_code == 200
    run_id = c.post("/api/runs", json={"budget": 15}).json()["id"]
    done = finished(stack, run_id)
    assert done["status"] == "done" and done["end_reason"] == "idle", done
    assert not any(i["kind"] == "control" and i["text"].startswith("Nudged") for i in stack.timeline(run_id))


def test_a_run_that_stops_without_idle_is_nudged_once(stack: Stack):
    c = stack.client
    assert c.put("/api/instructions", json={"text": "fake:noidle"}).status_code == 200
    run_id = c.post("/api/runs", json={"budget": 15}).json()["id"]
    done = finished(stack, run_id)
    texts = [i["text"] for i in stack.timeline(run_id)]
    nudges = [i["text"] for i in stack.timeline(run_id) if i["kind"] == "control" and i["text"].startswith("Nudged to carry on")]
    assert len(nudges) == 1, texts
    assert done["status"] == "done" and done["end_reason"] == "idle", done      # the resumed session ended with IDLE
    assert any(t.startswith("Brief: ") for t in texts)
