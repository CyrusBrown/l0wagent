"""M4 end to end on the local stack (runner + fake claude + egress): a wakeup that checks
its budget, asks the reviewer, registers a long job and plans the next wakeup; a Reply-now
run promoted to a full run; and a run stopped at its budget."""

import pytest

from .stack import Stack


def up(tmp_path, **kw) -> Stack:
    s = Stack(tmp_path, steps=4, delay=0.2, **kw).up()
    s.login()
    return s


@pytest.fixture
def stack(tmp_path):
    s = up(tmp_path, runner_env={"FAKE_JOB_S": "2"})
    yield s
    s.down()


def test_a_wakeup_plans_the_next_one_with_the_reviewer_and_a_job(stack: Stack):
    c = stack.client
    assert c.put("/api/instructions", json={"text": "fake:m4 Keep the garden exports nightly."}).status_code == 200
    run_id = c.post("/api/runs", json={"budget": 15}).json()["id"]
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="run to end")
    assert done["status"] == "done" and done["end_reason"] == "idle", done
    assert done["used"] > 0 and done["reviewer_mode"] == "monitor" and done["level"] == "steady"
    texts = [i["text"] for i in stack.timeline(run_id)]
    assert any(t.startswith("Reviewer: Nightly") for t in texts), "\n".join(texts)      # the reviewer answered its question
    wake_out = [i["data"].get("result") for i in stack.timeline(run_id) if i["kind"] == "tool" and "wake at" in i["text"]]
    assert any(t.startswith("Planned a wakeup") for t in texts), wake_out
    job_out = [i["data"].get("result") for i in stack.timeline(run_id) if i["kind"] == "tool" and "job start" in i["text"]]
    assert any(t.startswith("Registered a long job") for t in texts), job_out
    # The plan is on the calendar, the job on the jobs list (lost once its process ends without `job done`).
    cal = c.get("/api/calendar", params={"start": "2020-01-01T00:00:00Z", "end": "2020-02-01T00:00:00Z"})
    assert cal.status_code == 200
    planned = [w for w in c.get("/api/calendar").json()["wakeups"] + c.get("/api/calendar", params={"start": done["started_at"]}).json()["wakeups"]
               if w["reason"] == "Check the sensor export and summarize it"]
    assert planned and planned[0]["created_by"] == "assistant" and planned[0]["budget"] == 12
    jobs = c.get("/api/jobs").json()
    assert jobs[0]["label"] == "Nightly sensor export" and "rows 0..5000" in jobs[0]["log_tail"]
    # Its spend is in the ledger: the budget shows it as the assistant's.
    assert c.get("/api/budget").json()["providers"][0]["used_by_assistant"] > 0
    detail = c.get(f"/api/runs/{run_id}").json()
    assert detail["reviewer_mode"] == "monitor" and any("Planned a wakeup" in t for t in detail["changes"]["threads"])


def test_reply_now_promoted_to_a_full_run(stack: Stack):
    c = stack.client
    t = c.post("/api/threads", json={"title": "Pumps", "first_message": "fake:promote The pumps double-trigger.", "idempotency_key": "k-promote-1"}).json()
    d = c.get(f"/api/threads/{t['id']}/delivery").json()
    assert d["reply_now_available"] and d["reply_now_cost"] == 6
    run = c.post(f"/api/threads/{t['id']}/reply-now").json()
    assert run["bucket"] == "reply" and run["budget"] == 6
    pr = stack.wait_for(lambda: next((e for e in c.get("/api/inbox").json() if e["kind"] == "promote"), None), timeout=60, what="promote card")
    assert pr["promote"]["budget"] == 5
    assert c.post(f"/api/promote-requests/{pr['promote']['id']}/decide", json={"decision": "approve"}).status_code == 200
    r = stack.run(run["id"])
    assert (r["bucket"], r["why"]) == ("allowance", "promoted") and r["budget"] >= 5
    done = stack.wait_for(lambda: (x := stack.run(run["id"]))["status"] not in ("starting", "running") and x, timeout=90, what="run to end")
    assert done["status"] == "done", done
    texts = [i["text"] for i in stack.timeline(run["id"])]
    assert any(t.startswith("Promoted to a full run") for t in texts)


def test_a_run_stops_at_its_budget(tmp_path):
    s = Stack(tmp_path, steps=40, delay=0.5, runner_env={"FAKE_USAGE_SCALE": "2000"}).up()
    s.login()
    try:
        run_id = s.client.post("/api/runs", json={"budget": 3}).json()["id"]
        done = s.wait_for(lambda: (r := s.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="run to stop")
        assert done["status"] == "stopped" and done["end_reason"] == "budget", done
        assert done["used"] >= 3
    finally:
        s.down()
