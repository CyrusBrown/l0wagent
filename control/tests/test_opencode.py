"""opencode Go (M6) on the local stack (runner + fake opencode + egress), with no Claude login
and the usage gate refusing unknown usage: a friend with only an opencode Go key.

Also the pieces on their own: the adapter's parsing of opencode's events, the runner's
result line, tier → model/variant mapping, and the provider's key and models."""

import json
import sys
from pathlib import Path

import pytest

from l0w_control.budgets import GO_DEFAULT_MODELS, pick_variant, price, split_tier
from l0w_control.runs.runtime import ADAPTERS

from .stack import REPO, Stack

sys.path.insert(0, str(REPO / "agent" / "runner"))
import runner  # noqa: E402

GO_KEY = "sk-go-test-0123456789abcdefghij"

# A real `opencode run --format json` session (opencode 1.18.31, opencode-go/beam-free), trimmed.
REAL = [
    {"type": "step_start", "sessionID": "ses_a1", "part": {"id": "prt_1", "type": "step-start"}},
    {"type": "tool_use", "sessionID": "ses_a1", "part": {"type": "tool", "tool": "read", "callID": "c1",
                                                          "state": {"status": "completed", "input": {"filePath": "a.txt"}, "output": "1: hello"}}},
    {"type": "step_finish", "sessionID": "ses_a1", "part": {"id": "prt_2", "reason": "tool-calls", "type": "step-finish",
                                                             "tokens": {"total": 9964, "input": 8064, "output": 13, "reasoning": 31, "cache": {"write": 0, "read": 1856}}, "cost": 0.0021}},
    {"type": "step_start", "sessionID": "ses_a1", "part": {"id": "prt_3", "type": "step-start"}},
    {"type": "text", "sessionID": "ses_a1", "part": {"id": "prt_4", "type": "text", "text": "IDLE: hello"}},
    {"type": "step_finish", "sessionID": "ses_a1", "part": {"id": "prt_5", "reason": "stop", "type": "step-finish",
                                                             "tokens": {"input": 149, "output": 6, "reasoning": 18, "cache": {"write": 0, "read": 9920}}, "cost": 0.0004}},
]


def test_the_adapter_reads_opencode_events():
    oc = ADAPTERS["opencode"]
    p = oc.parse(REAL[1])
    assert p.session_id == "ses_a1" and p.items[0].kind == "tool" and p.items[0].text == "read a.txt"
    assert p.items[0].data["result"] == "1: hello" and p.items[0].data["is_error"] is False
    p = oc.parse(REAL[2])
    assert p.usage and p.usage[0] == "prt_2" and p.usage[2]["cost_usd"] == 0.0021 and p.usage[2]["output_tokens"] == 44
    assert price(None, p.usage[2]) == 0.0021
    assert oc.parse(REAL[4]).items[0].text == "IDLE: hello"
    err = oc.parse({"type": "error", "sessionID": "ses_a1", "error": {"name": "UnknownError", "data": {"message": "Model not found"}}})
    assert err.items[0].kind == "error" and "Model not found" in err.items[0].text
    bash = oc.parse({"type": "tool_use", "part": {"tool": "bash", "callID": "c2", "state": {"status": "completed", "input": {"command": "ls"}, "output": ""}}})
    assert bash.items[0].text == "$ ls"


def test_the_runner_writes_the_result_line_opencode_lacks():
    r = runner.OpencodeResult(None)
    for ev in REAL:
        r.see(ev)
    line = r.line(0)
    assert line["result"] == "IDLE: hello" and not line["is_error"] and line["session_id"] == "ses_a1"
    assert line["num_turns"] == 2 and line["total_cost_usd"] == 0.0025 and line["usage"]["cache_read_input_tokens"] == 11776
    parsed = ADAPTERS["opencode"].parse(line)
    assert parsed.result and parsed.result["ok"] and parsed.result["cost_usd"] == 0.0025
    assert runner.OpencodeResult("ses_x").line(130)["is_error"]
    failed = runner.OpencodeResult(None)
    failed.see({"type": "error", "error": {"data": {"message": "Unexpected server error"}}})
    assert failed.line(1)["result"] == "Unexpected server error"


def test_the_runner_builds_opencodes_config_and_command(tmp_path: Path):
    sys_file = tmp_path / "system.md"
    cfg = runner.opencode_config(sys_file)
    assert cfg["agent"]["l0w"]["prompt"] == "{file:" + str(sys_file) + "}" and cfg["permission"]["*"] == "allow"
    assert cfg["plugin"][0].endswith("opencode-plugin.js") and cfg["autoupdate"] is False and cfg["share"] == "disabled"
    review = runner.opencode_config(sys_file, reviewer=True)
    assert "plugin" not in review and review["permission"]["edit"] == "deny"
    argv = runner.opencode_argv("opencode-go/glm-5.3", "high", "ses_1")
    assert argv[1:] == ["run", "--format", "json", "--auto", "-m", "opencode-go/glm-5.3", "--agent", "l0w", "--variant", "high", "--session", "ses_1"]


def test_tiers_split_into_model_and_effort():
    assert split_tier("sonnet/medium") == ("sonnet", "medium")
    assert split_tier("opencode-go/glm-5.3/high") == ("opencode-go/glm-5.3", "high")
    assert split_tier("opencode-go/glm-5.3") == ("opencode-go/glm-5.3", None)


def test_efforts_map_to_the_closest_variant():
    assert pick_variant("medium", ["low", "high", "max"]) == "high"   # ties go up
    assert pick_variant("low", ["high", "max"]) == "high"
    assert pick_variant("high", ["low", "high"]) == "high"
    assert pick_variant("medium", []) is None and pick_variant(None, ["low"]) is None


# ── on the stack ──────────────────────────────────────────────────────────
@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path, steps=3, delay=0.2, usage_policy="refuse").up()
    s.login()
    yield s
    s.down()


def add_go(c) -> dict:
    r = c.post("/api/providers", json={"key": GO_KEY, "kind": "opencode-go", "use_for_all_roles": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_go_only_instance_runs_wakeups_on_opencode(stack: Stack):
    c = stack.client
    # Without a Claude login nothing starts (the gate fails closed)…
    assert c.post("/api/runs", json={"budget": 10}).status_code == 409
    info = add_go(c)
    assert info["runtime"] == "opencode" and [m["id"] for m in info["models"]] == GO_DEFAULT_MODELS
    assert GO_KEY not in json.dumps(info)
    roles = c.get("/api/roles").json()["roles"]
    assert {r["provider"] for r in roles} == {"opencode-go"}
    assert next(r for r in roles if r["role"] == "reviewer")["tier"] == "opencode-go/glm-5.3/medium"  # opus/medium → the largest enabled model
    # …with Go, a wakeup runs on opencode.
    run_id = c.post("/api/runs", json={"budget": 10}).json()["id"]
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="run to end")
    assert done["status"] == "done" and done["end_reason"] == "idle", done
    assert done["provider"] == "opencode-go" and done["used"] > 0
    items = stack.timeline(run_id)
    texts = [i["text"] for i in items]
    assert any(t.startswith("Model opencode-go/") for t in texts), texts
    assert any(i["kind"] == "tool" and i["data"].get("result") for i in items)
    assert any(t.startswith("Session finished: IDLE") for t in texts), texts
    budget = next(p for p in c.get("/api/budget").json()["providers"] if p["provider"] == "opencode-go")
    assert budget["used_by_assistant"] > 0 and budget["native"].endswith("this month") and budget["period"] == "month"
    # The owner picks which models the assistant may use; roles name one directly, with an effort.
    r = c.patch("/api/providers/opencode-go", json={"models": ["opencode-go/beam-free", "opencode-go/glm-5.3"]})
    assert r.status_code == 200 and [m["id"] for m in r.json()["models"]] == ["opencode-go/beam-free", "opencode-go/glm-5.3"]
    assert c.patch("/api/providers/opencode-go", json={"models": ["glm"]}).status_code == 400
    worker = next(x for x in c.get("/api/roles").json()["roles"] if x["role"] == "worker")
    assert "opencode-go/beam-free" in worker["allowed"]
    roles = [{**x, "tier": "opencode-go/beam-free/high"} if x["role"] == "worker" else x for x in c.get("/api/roles").json()["roles"]]
    assert c.put("/api/roles", json={"roles": roles}).status_code == 200
    run2 = c.post("/api/runs", json={"budget": 10, "tier": "opencode-go/beam-free/high"}).json()["id"]
    stack.wait_for(lambda: stack.run(run2)["status"] not in ("starting", "running"), timeout=90, what="second run")
    assert any(i["text"] == "Model opencode-go/beam-free" for i in stack.timeline(run2))


def test_reply_now_on_go_hears_the_answer_mid_run(stack: Stack):
    c = stack.client
    add_go(c)
    t = c.post("/api/threads", json={"title": "Pump test", "first_message": "Can you run the pump test this weekend?"}).json()
    run = c.post(f"/api/threads/{t['id']}/reply-now")
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    q = stack.wait_for(lambda: next((i["question"] for i in c.get(f"/api/threads/{t['id']}/items").json()["items"] if i["type"] == "question"), None), what="the question")
    assert c.post(f"/api/questions/{q['id']}/answer", json={"value": "Sunday"}).status_code == 200
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] != "running" and r, timeout=60, what="run to end")
    assert done["status"] == "done" and done["provider"] == "opencode-go"
    texts = [i["text"] for i in stack.timeline(run_id)]
    assert any(x.startswith("Delivered mid-run (after a tool call): 1 answer") for x in texts), texts
