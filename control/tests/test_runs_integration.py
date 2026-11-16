"""M1's main flow, end to end: log in, start a run, watch it live, restart control mid-run,
and the run carries on. Also: the agent computer restarting mid-run, stopping, the gate."""

import signal

import pytest

from .stack import Stack


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path).up()
    s.login()
    yield s
    s.down()


def tools(items: list[dict]) -> list[str]:
    return [i["text"] for i in items if i["kind"] == "tool"]


def test_run_survives_a_control_restart(stack: Stack):
    r = stack.client.post("/api/runs", json={})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]

    # Live: the agent's own reports arrive while it works.
    stack.wait_for(lambda: stack.run(run_id)["status"] == "running", what="running")
    stack.wait_for(lambda: len(tools(stack.timeline(run_id))) >= 3, what="three tool calls")
    # The agent's note (step 2) must land before control goes down: a CLI call made while
    # control is down fails, by design.
    stack.wait_for(lambda: any(i["kind"] == "note" for i in stack.timeline(run_id)), what="the agent's note")
    assert stack.lease_active()

    stack.kill("control")
    stack.start_control()
    stack.login()

    done = stack.wait_for(lambda: (run := stack.run(run_id))["status"] != "running" and run, timeout=60, what="run to end")
    assert done["status"] == "done", done
    assert done["about"].startswith("A fake session")
    items = stack.timeline(run_id)
    texts = [i["text"] for i in items]
    assert "Control restarted; re-attached to the run" in texts
    # Every step stored exactly once, across the restart.
    assert len(tools(items)) == 1 + stack.steps
    assert any(i["kind"] == "note" and i["text"] == "Journal index tidied." for i in items)
    tool_items = [i for i in items if i["kind"] == "tool"]
    assert all("result" in i["data"] for i in tool_items), "tool results attach to their calls"
    stack.wait_for(lambda: not stack.lease_active(), timeout=10, what="lease released")
    simple = stack.timeline(run_id, "simple")
    assert all(i["simple"] for i in simple) and len(simple) < len(items)


def test_run_resumes_after_the_agent_computer_restarts(stack: Stack):
    run_id = stack.client.post("/api/runs", json={}).json()["id"]
    stack.wait_for(lambda: len(tools(stack.timeline(run_id))) >= 3, what="three tool calls")
    stack.kill("runner")
    stack.start_runner()
    done = stack.wait_for(lambda: (run := stack.run(run_id))["status"] != "running" and run, timeout=90, what="run to end")
    texts = [i["text"] for i in stack.timeline(run_id)]
    assert "The agent computer restarted mid-session" in texts
    assert "Resumed the conversation after the agent computer restarted" in texts
    assert done["status"] == "done", texts
    assert any(t.startswith("Resuming: [control] Your session was interrupted") for t in texts)


def test_stop(stack: Stack):
    run_id = stack.client.post("/api/runs", json={}).json()["id"]
    stack.wait_for(lambda: len(tools(stack.timeline(run_id))) >= 2, what="two tool calls")
    assert stack.client.post(f"/api/runs/{run_id}/stop").status_code == 200
    done = stack.wait_for(lambda: (run := stack.run(run_id))["status"] not in ("running", "starting") and run, timeout=30, what="stop")
    assert done["status"] == "stopped"
    assert stack.client.post("/api/runs", json={}).status_code == 201, "a new run can start after a stop"


def test_one_run_at_a_time(stack: Stack):
    assert stack.client.post("/api/runs", json={}).status_code == 201
    r = stack.client.post("/api/runs", json={})
    assert r.status_code == 409 and r.json()["error"] == "busy"


def test_usage_gate_fails_closed(tmp_path):
    s = Stack(tmp_path, usage_policy="refuse").up()
    try:
        s.login()
        r = s.client.post("/api/runs", json={})
        assert r.status_code == 409 and r.json()["error"] == "gate_closed"
        assert "can't be read" in r.json()["detail"]
        system = s.client.get("/api/system").json()
        assert system["gate"]["open"] is False and system["runner"]["reachable"] and system["egress"]["reachable"]
    finally:
        s.down()


def test_sigterm_on_control_leaves_the_run_running(stack: Stack):
    """A graceful control shutdown (an update) doesn't stop runs either."""
    run_id = stack.client.post("/api/runs", json={}).json()["id"]
    stack.wait_for(lambda: len(tools(stack.timeline(run_id))) >= 2, what="two tool calls")
    stack.kill("control", signal.SIGTERM)
    stack.start_control()
    stack.login()
    done = stack.wait_for(lambda: (run := stack.run(run_id))["status"] != "running" and run, timeout=60, what="run to end")
    assert done["status"] == "done"


# ── M2 ──────────────────────────────────────────────────────────────────────
def items(stack: Stack, thread_id: int) -> list[dict]:
    return stack.client.get(f"/api/threads/{thread_id}/items").json()["items"]


def bodies(stack: Stack, thread_id: int) -> list[str]:
    return [i["message"]["body"] for i in items(stack, thread_id) if i["type"] == "message"]


def test_reply_now_claims_asks_and_hears_the_answer(stack: Stack):
    """M2's done-when: message the assistant, press Reply now, and it replies, claims a task
    and files a question you answer in place; the answer reaches it mid-run (the hook)."""
    secret = "sk-test-0123456789abcdef"
    assert stack.client.put("/api/secrets/PUMP_API_KEY", json={"value": secret}).status_code == 200
    t = stack.client.post("/api/threads", json={"title": "Pump test", "first_message": "Can you run the pump test this weekend?"}).json()
    assert stack.client.get(f"/api/threads/{t['id']}/delivery").json()["reply_now_available"]
    run = stack.client.post(f"/api/threads/{t['id']}/reply-now")
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    assert run.json()["kind"] == "reply"
    assert stack.client.post(f"/api/threads/{t['id']}/reply-now").status_code == 409

    q = stack.wait_for(lambda: next((i["question"] for i in items(stack, t["id"]) if i["type"] == "question"), None), what="the question")
    assert q["options"] == ["Saturday", "Sunday"] and q["default"] == "Saturday"
    claim = next(i["claim"] for i in items(stack, t["id"]) if i["type"] == "claim")
    assert claim["status"] == "in_progress" and claim["run_id"] == run_id
    assert "On it. One question first, so I schedule it right." in bodies(stack, t["id"])
    assert stack.client.get(f"/api/threads/{t['id']}/delivery").json()["state"] == "working"

    assert stack.client.post(f"/api/questions/{q['id']}/answer", json={"value": "Sunday"}).status_code == 200
    stack.wait_for(lambda: "Got it: Sunday. The pump test is scheduled." in bodies(stack, t["id"]), what="the reply using the answer")
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] != "running" and r, timeout=60, what="run to end")
    assert done["status"] == "done"
    claim = next(i["claim"] for i in items(stack, t["id"]) if i["type"] == "claim")
    assert claim["status"] == "done" and claim["result"] == "Pump test scheduled for Sunday."
    texts = [i["text"] for i in stack.timeline(run_id)]
    assert any(x.startswith("Delivered in the brief: 1 message") for x in texts)
    assert any(x.startswith("Delivered mid-run (after a tool call): 1 answer") for x in texts)
    # The session got the secret; nothing control stored or shows contains it.
    assert "Secrets in my environment: PUMP_API_KEY" in texts
    raw = stack.client.get(f"/api/runs/{run_id}/timeline", params={"view": "detailed"}).text
    assert secret not in raw and "[secret:PUMP_API_KEY]" in raw
    import sqlite3

    db = sqlite3.connect(stack.tmp / "control" / "control.db")
    assert not db.execute("SELECT COUNT(*) FROM run_events WHERE raw_json LIKE ?", (f"%{secret}%",)).fetchone()[0]
    detail = stack.client.get(f"/api/runs/{run_id}").json()
    assert detail["claims"][0]["status"] == "done" and detail["changes"]["asks"] == ["Question: Which day should I run the pump test?"]


def test_run_holds_open_for_a_reply(stack: Stack):
    t = stack.client.post("/api/threads", json={"title": "Plans", "first_message": "Here's a thought for later."}).json()
    run_id = stack.client.post("/api/runs", json={}).json()["id"]
    stack.wait_for(lambda: "Thanks, I've read this. Anything else before I start?" in bodies(stack, t["id"]), what="the agent's reply")
    stack.wait_for(lambda: any(i["text"].startswith("Waiting up to") for i in stack.timeline(run_id)), what="holding open")
    assert stack.run(run_id)["status"] == "running"
    assert stack.client.get(f"/api/threads/{t['id']}/delivery").json()["text"].startswith("Waiting for your reply")
    stack.client.post(f"/api/threads/{t['id']}/messages", json={"body": "No, go ahead."})
    stack.wait_for(lambda: "Thanks for the reply, that settles it." in bodies(stack, t["id"]), what="the resumed reply")
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] != "running" and r, timeout=90, what="run to end")
    assert done["status"] == "done"
    texts = [i["text"] for i in stack.timeline(run_id)]
    assert any(x.startswith("Resumed with your reply: 1 message") for x in texts)


def test_permission_egress_rule_applies_through_egress(stack: Stack):
    import httpx

    from .stack import TOKENS

    agent = {"X-Agent-Token": TOKENS["agent"]}
    p = stack.client.post("/api/agent/permissions", headers=agent, json={"text": "Read the weather", "actions": [{"kind": "egress_rule", "pattern": "api.weather.example"}]}).json()
    assert stack.client.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "always"}).status_code == 200
    rules = httpx.get(f"http://127.0.0.1:{stack.ports['admin']}/rules", headers={"X-Egress-Token": TOKENS["egress"]}).json()
    assert [(r["pattern"], r["action"]) for r in rules] == [("api.weather.example", "allow_ro")]


# ── M3 ──────────────────────────────────────────────────────────────────────
def test_upload_reaches_the_agent_and_its_live_shelf_item_updates(stack: Stack):
    """M3's done-when: an uploaded file reaches the assistant; it shelves a script's output,
    and the item updates on the project's Files tab when the script reruns."""
    assert stack.client.post("/api/projects", json={"name": "Garden"}).status_code == 201
    up = stack.client.post("/api/uploads", files={"file": ("readings.csv", b"sensor,moisture\na,30\nb,42\n")}, data={"project": "garden"}).json()
    assert up["path"] == "_inbox/garden/readings.csv"
    t = stack.client.post("/api/threads", json={"title": "Readings", "project": "garden", "first_message": "Summarize these, please.",
                                                "upload_ids": [up["id"]]}).json()
    run_id = stack.client.post(f"/api/threads/{t['id']}/reply-now").json()["id"]

    item = stack.wait_for(lambda: next(iter(stack.client.get("/api/projects/garden/shelf").json()), None), what="the shelf item")
    assert item["mode"] == "live" and item["added_by"] == "assistant" and item["keep_versions"] == 3 and item["run_id"] == run_id
    assert stack.client.get(f"/api/shelf/{item['id']}/raw").text == "first pass: 2 readings, total 72\n"
    stack.wait_for(lambda: stack.client.get(f"/api/shelf/{item['id']}/raw").text.startswith("second pass"), what="the rerun's output")
    versions = stack.wait_for(lambda: (v := stack.client.get(f"/api/shelf/{item['id']}/versions").json()) and len(v) == 2 and v, what="two versions")
    assert versions[0]["run_id"] == run_id
    stack.wait_for(lambda: stack.run(run_id)["status"] == "done", timeout=60, what="run to end")
    # The agent read the upload (its content is in the run's timeline).
    tools = [i for i in stack.timeline(run_id) if i["kind"] == "tool"]
    assert any("sensor,moisture" in (i["data"].get("result") or "") for i in tools)
    assert "Re-ran the script; the summary on the shelf is updated." in bodies(stack, t["id"])
    # And it's browsable, with the shelf item's bytes on the files origin.
    listing = stack.client.get("/api/files/list", params={"volume": "work", "path": "garden/out"}).json()
    assert [e["name"] for e in listing["entries"]] == ["summary.txt"]
    url = stack.client.get(f"/api/shelf/{item['id']}/preview-url").json()["url"]
    import httpx

    assert url.startswith(f"http://127.0.0.1:{stack.ports['files']}/f/")
    r = httpx.get(url)
    assert r.status_code == 200 and r.text.startswith("second pass")
