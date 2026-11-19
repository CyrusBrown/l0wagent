#!/usr/bin/env python3
"""A stand-in for `claude -p --output-format stream-json`, for development and tests.

The real runner runs it in place of Claude Code (CLAUDE_BIN=.../fake_claude.py, or
FAKE_CLAUDE=1 in the agent image), so everything from the runner to the UI is exercised
without a login or model usage. It writes a plausible session, step by step, and keeps
its conversations on disk so `--resume` continues one.

  FAKE_STEPS=6            steps per session
  FAKE_STEP_DELAY=1.5     seconds between steps
  FAKE_CALL_ASSISTANT=1   really run `assistant status/note` (needs CONTROL_URL + AGENT_API_TOKEN)
  FAKE_FAIL=1             end with an error result
  FAKE_WAIT_S=90          how long a Reply-now session waits for the answer to its question
  FAKE_USAGE_SCALE=1      multiplies each message's token usage (to exercise budgets)

Scenarios (with FAKE_CALL_ASSISTANT=1), picked from the prompt:
  Reply now + file    an attachment in the brief: read it, write and run a script, shelve its
                      output (live, keeping versions), then rerun the script so the output changes
  Reply now           read the thread, claim it, reply, ask a question, wait for the answer
                      (delivered by the real assistant-hook after a tool call), answer, close the claim
  Reply now + promote "fake:promote" in the thread: ask to promote the reply, wait for the decision,
                      then do the work (approved) or claim the thread and plan a wakeup (denied)
  wakeup "fake:m4"    (e.g. in the owner's standing instructions) check the budget, ask the reviewer,
                      register a long job, plan the next wakeup, end IDLE
  wakeup "fake:wrapup" / "fake:noidle"   end with a wrap-up line then IDLE:, or without IDLE at all
                      (control nudges that once; the resumed session then ends IDLE)
  wakeup with mail    reply to the owner's queued messages (so control holds the run open)
  resumed for reply   control resumed the session with the owner's reply: answer it
  otherwise           the M1 session: tidy the journal
After every tool call it runs `assistant-hook` like Claude Code's PostToolUse hook does.
With ASSISTANT_RUN_MODE=reviewer (the runner's /consult) it plays the reviewer instead: QUIET on
watch updates, AGREE to ending, an answer to questions, and `LEVEL: opus/high` for "fake:level".
With `--strict-mcp-config --mcp-config FILE` (the chat sidecar) it plays a chat turn: a short reply, or the result of the
tool named by [fake:tool NAME {json-args}] in the prompt, called on the "l0w" MCP server over HTTP.
Every model message carries token usage, and the result reports the conversation's total cost so far: like Claude Code,
a resumed conversation's total includes its earlier turns.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

STATE = Path(os.environ.get("FAKE_CLAUDE_STATE", str(Path.home() / ".fake-claude")))
STEPS = int(os.environ.get("FAKE_STEPS", "6"))
DELAY = float(os.environ.get("FAKE_STEP_DELAY", "1.5"))
CALL_ASSISTANT = os.environ.get("FAKE_CALL_ASSISTANT") == "1"
USAGE_SCALE = float(os.environ.get("FAKE_USAGE_SCALE", "1"))
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
MODEL = {"name": "claude-sonnet-fake"}
COST = {"usd": 0.0}


def usage() -> dict:
    """One model message's usage, priced the way control prices it, added to the session's cost."""
    u = {"input_tokens": int(3000 * USAGE_SCALE), "output_tokens": int(400 * USAGE_SCALE), "cache_read_input_tokens": int(20000 * USAGE_SCALE)}
    pin, pout = next((v for k, v in PRICES.items() if k in MODEL["name"]), PRICES["sonnet"])
    COST["usd"] += (u["input_tokens"] * pin + u["output_tokens"] * pout + u["cache_read_input_tokens"] * pin * 0.1) / 1e6
    return u


def message(content: list) -> dict:
    return {"id": f"msg_{uuid.uuid4().hex[:12]}", "model": MODEL["name"], "content": content, "usage": usage()}

WORK = [
    ("Reading NOW.md to see what's in flight.", "Read", {"file_path": "/workspace/NOW.md"}, "# Now\n\nNothing in flight yet."),
    ("Checking the backlog.", "Bash", {"command": "cat plan/backlog.md", "description": "read the backlog"}, "# Backlog\n\n- tidy the journal"),
    ("Tidying the journal index.", "Edit", {"file_path": "/workspace/journal/README.md", "old_string": "x", "new_string": "y"}, "The file has been updated."),
    ("Running the tests for the garden script.", "Bash", {"command": "pytest -q", "description": "run tests"}, "4 passed in 0.31s"),
    ("Writing today's journal entry.", "Write", {"file_path": "/workspace/journal/2026/11-15.md", "content": "## Run\n- did things"}, "File created."),
    ("Updating NOW.md for the next session.", "Write", {"file_path": "/workspace/NOW.md", "content": "# Now\n\nJournal tidied."}, "File written."),
]


CONV_FILE: dict[str, Path | None] = {"path": None}  # the main conversation's state file, where its total is kept


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    path = CONV_FILE["path"]
    if obj.get("type") == "result" and path is not None and path.exists():
        conv = json.loads(path.read_text())
        conv["cost_usd"] = COST["usd"]
        path.write_text(json.dumps(conv))


def arg(name: str) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else None


def assistant(*args: str) -> str:
    if not CALL_ASSISTANT:
        return "ok"
    try:
        r = subprocess.run(["assistant", *args], capture_output=True, text=True, timeout=20)
        return (r.stdout + r.stderr).strip() or f"exit {r.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"assistant failed: {e}"


INTERRUPTED = {"flag": False}


class Stopped(Exception):
    pass


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.n = 0
        self.context: list[str] = []  # what the hook delivered

    def say(self, text: str) -> None:
        emit({"type": "assistant", "message": message([{"type": "text", "text": text}]), "session_id": self.sid})

    def run(self, *args: str, show: str | None = None) -> str:
        """One Bash tool call running `assistant …`, then the PostToolUse hook."""
        if INTERRUPTED["flag"]:
            raise Stopped
        self.n += 1
        tid = f"tu_{self.n}_{uuid.uuid4().hex[:6]}"
        cmd = show or "assistant " + " ".join(a if " " not in a else repr(a) for a in args)
        emit({"type": "assistant", "message": message([{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]), "session_id": self.sid})
        out = assistant(*args)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": out}]}, "session_id": self.sid})
        self.hook()
        time.sleep(DELAY)
        return out

    def bash(self, command: str, output: str) -> None:
        self.n += 1
        tid = f"tu_{self.n}_{uuid.uuid4().hex[:6]}"
        emit({"type": "assistant", "message": message([{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]), "session_id": self.sid})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": output}]}, "session_id": self.sid})
        self.hook()

    def hook(self) -> str:
        if not CALL_ASSISTANT:
            return ""
        try:
            r = subprocess.run(["assistant-hook"], input="{}", capture_output=True, text=True, timeout=10)
            ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            ctx = ""
        if ctx:
            self.context.append(ctx)
        return ctx

    def finish(self, text: str) -> int:
        self.say(text)
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": self.sid, "num_turns": self.n,
              "total_cost_usd": round(COST["usd"], 6), "usage": {"input_tokens": 900 * self.n, "output_tokens": 200 * self.n}})
        return 0


def show_secrets(sess: Session) -> None:
    """Prove the session got the secrets (names only in text), and echo one so the
    redaction of tool output can be checked."""
    names = [line.split()[0] for line in assistant("secret", "list").splitlines() if line and line.split()[0].isupper() and line.split()[0] in os.environ]
    if names:
        sess.say("Secrets in my environment: " + ", ".join(names))
        sess.bash(f"echo ${names[0]}", os.environ[names[0]])


def promote_scenario(sess: Session, thread: int) -> int:
    sess.run("status", "Reading the thread")
    sess.run("send", "--thread", str(thread), "This needs a proper run, not a quick reply. Asking to promote it.")
    out = sess.run("promote", "--budget", "5", "--reason", "Audit every pump schedule and fix the double trigger")
    pid = (re.search(r"promote request #(\d+)", out) or [None, "0"])[1]
    decision, deadline = None, time.time() + float(os.environ.get("FAKE_WAIT_S", "90"))
    while decision is None and time.time() < deadline:
        for ctx in sess.context:
            m = re.search(rf"Promote request #{pid} .*?: (approved|denied|not answered)", ctx)
            if m:
                decision = m.group(1)
        if decision is None:
            if INTERRUPTED["flag"]:
                raise Stopped
            time.sleep(1)
            sess.hook()
    if decision == "approved":
        sess.run("claim", str(thread), "Audit the pump schedules and fix the double trigger")
        sess.run("note", "Promoted: auditing the schedules")
        sess.run("send", "--thread", str(thread), "Done: the double trigger came from overlapping schedules; fixed.")
        sess.run("claim", "done", str(thread), "Overlapping schedules removed.")
        return sess.finish("IDLE: promoted run finished.")
    sess.run("claim", str(thread), "Audit the pump schedules in the next wakeup")
    sess.run("wake", "at", (time.strftime("%Y-%m-%d", time.gmtime(time.time() + 2 * 86400)) + "T03:00"), "--budget", "10", "--reason", "Audit the pump schedules")
    sess.run("send", "--thread", str(thread), "I'll do this in tonight's wakeup.")
    return sess.finish("IDLE: claimed and planned for a wakeup.")


def m4_scenario(sess: Session, prompt: str) -> int:
    """A wakeup that uses M4: the budget, the reviewer, a long job and the wakeup plan."""
    sess.run("run", "describe", "Check the budget, run the nightly sensor export as a long job, and plan the next wakeup.")
    sess.run("status", "Checking the budget")
    sess.run("budget")
    if "reviewer:** off" not in prompt:
        sess.run("reviewer", "ask", "Should the sensor export run nightly or weekly?")
    sess.run("status", "Starting the sensor export")
    log = f"{WORK_ROOT}/garden/export.log"
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "w") as f:
        f.write("export started\nrows 0..5000\n")
    proc = subprocess.Popen(["sleep", os.environ.get("FAKE_JOB_S", "30")], start_new_session=True)
    sess.run("job", "start", "Nightly sensor export", "--pid", str(proc.pid), "--log", log.replace(WORK_ROOT, "/work"), "--expect", "1h")
    later = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 2 * 86400))  # owner's local date may differ by up to a day
    note = f"{WORK_ROOT}/garden/next-wakeup.md"
    with open(note, "w") as f:
        f.write("The export job is registered; read export.log first. Don't restart the export.\n")
    sess.run("wake", "at", f"{later}T03:00", "--budget", "12", "--tier", "sonnet/medium", "--reason", "Check the sensor export and summarize it",
             "--note", note)
    sess.run("note", "Export running; next wakeup planned")
    return sess.finish("IDLE: export running as a job; next wakeup planned.")


def reviewer_turn(prompt: str) -> int:
    """Play the reviewer for one /consult turn."""
    sid = os.environ.get("FAKE_REVIEWER_SESSION") or arg("--resume") or str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": MODEL["name"]})
    if "fake:level" in prompt:
        text = "This needs more care than sonnet gives it.\nLEVEL: opus/high"
    elif "wants to end" in prompt:
        text = "Not yet: the export summary isn't written." if "fake:disagree" in prompt and "fake:disagreed" not in prompt else "Fair: nothing worthwhile is left.\nAGREE"
    elif "says a task" in prompt:
        text = "Checked the diff; it's tested.\nAGREE"
    elif "asks:" in prompt:
        text = "Nightly: the sensors drift within a day, and the export is cheap."
    elif "starting a task" in prompt:
        text = "QUIET"
    else:
        text = "QUIET"
    emit({"type": "assistant", "message": message([{"type": "text", "text": text}]), "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "num_turns": 1, "total_cost_usd": round(COST["usd"], 6)})
    return 0


def reply_scenario(sess: Session, thread: int) -> int:
    sess.run("status", "Reading the thread")
    sess.run("messages", "--thread", str(thread))
    sess.run("claim", str(thread), "Pick a day for the pump test and schedule it")
    sess.run("send", "--thread", str(thread), "On it. One question first, so I schedule it right.")
    out = sess.run("question", "Which day should I run the pump test?", "--kind", "single", "-o", "Saturday", "-o", "Sunday",
                   "--default", "Saturday", "--thread", str(thread))
    qid = (re.search(r"question #(\d+)", out) or [None, "0"])[1]
    sess.run("status", "Waiting for your answer")
    answer, deadline = None, time.time() + float(os.environ.get("FAKE_WAIT_S", "90"))
    while answer is None and time.time() < deadline:
        for ctx in sess.context:
            m = re.search(rf"question #{qid} .*?: (.+?)\.(?: Note|$)", ctx, re.M)
            if m:
                answer = m.group(1)
        if answer is None:
            if INTERRUPTED["flag"]:
                raise Stopped
            time.sleep(1)
            sess.hook()
    answer = answer or "Saturday (the default; no answer in time)"
    sess.run("send", "--thread", str(thread), f"Got it: {answer}. The pump test is scheduled.")
    sess.run("claim", "done", str(thread), f"Pump test scheduled for {answer}.")
    return sess.finish("IDLE: replied, the question is answered and the test is scheduled.")


WORK_ROOT = os.environ.get("WORK_DIR", "/work").rstrip("/")
SCRIPT = """import csv, sys
rows = list(csv.reader(open(sys.argv[1])))
data = rows[1:] if rows and not rows[0][-1].replace('.', '').isdigit() else rows
total = sum(float(r[-1]) for r in data if r and r[-1].replace('.', '').isdigit())
open(sys.argv[2], 'w').write(f"{sys.argv[3]}: {len(data)} readings, total {total:g}\\n")
"""


def files_scenario(sess: Session, thread: int, project: str, attached: str) -> int:
    local = WORK_ROOT + attached[len("/work"):] if attached.startswith("/work/") else attached
    sess.run("status", "Reading the file you attached")
    try:
        content = open(local).read(4000)
    except OSError as e:
        content = f"cat: {e}"
    sess.bash(f"cat {attached}", content)
    folder = f"{WORK_ROOT}/{project}"
    os.makedirs(f"{folder}/out", exist_ok=True)
    with open(f"{folder}/summarize.py", "w") as f:
        f.write(SCRIPT)
    out = f"{folder}/out/summary.txt"

    def run_script(label: str) -> None:
        r = subprocess.run([sys.executable, f"{folder}/summarize.py", local, out, label], capture_output=True, text=True)
        sess.bash(f"python3 {folder.replace(WORK_ROOT, '/work')}/summarize.py {attached} out/summary.txt", (r.stdout + r.stderr) or "ok")

    run_script("first pass")
    sess.run("shelf", "add", out, "--project", project, "--label", "Readings summary", "--keep", "3")
    sess.run("send", "--thread", str(thread), "I summarized the readings; the result is on the project's Files tab (it stays live).")
    time.sleep(float(os.environ.get("FAKE_RERUN_DELAY", "4")))
    run_script("second pass")
    sess.run("send", "--thread", str(thread), "Re-ran the script; the summary on the shelf is updated.")
    return sess.finish("IDLE: summarized the upload and shelved the output.")


def mail_scenario(sess: Session, prompt: str) -> int:
    threads = sorted(set(int(t) for t in re.findall(r"^thread #(\d+) ", prompt, re.M)))
    sess.run("status", "Answering your messages")
    for t in threads:
        sess.run("send", "--thread", str(t), "Thanks, I've read this. Anything else before I start?")
    return sess.finish("IDLE: answered the queued messages.")


def resumed_scenario(sess: Session, prompt: str) -> int:
    threads = sorted(set(int(t) for t in re.findall(r"^thread #(\d+) ", prompt, re.M)))
    for t in threads:
        sess.run("send", "--thread", str(t), "Thanks for the reply, that settles it.")
    return sess.finish("IDLE: answered the reply.")


def wrapup_scenario(sess: Session) -> int:
    """Ends with a short wrap-up and THEN the IDLE line (control must still see it as idle)."""
    sess.run("run", "describe", "Tidy up and say so, then end idle.")
    sess.run("status", "Wrapping up")
    return sess.finish("Journal tidied and the tests pass.\nIDLE: nothing else worth doing.")


def noidle_scenario(sess: Session) -> int:
    """Just stops, without IDLE, with budget left (control nudges it once)."""
    sess.run("run", "describe", "Do one small thing and stop without saying IDLE.")
    sess.run("status", "Doing one small thing")
    return sess.finish("Done for now.")


def nudged_scenario(sess: Session, prompt: str) -> int:
    sess.run("status", "Nudged to carry on")
    return sess.finish("Picked up the next thing.\nIDLE: nothing more worth doing.")


def scenario(sess: Session, prompt: str, resume: str | None) -> int | None:
    if not CALL_ASSISTANT:
        return None
    if prompt.startswith("[control] You have ") and "left of this run's budget" in prompt:
        return nudged_scenario(sess, prompt)
    if prompt.startswith("[control] The owner responded"):
        return resumed_scenario(sess, prompt)
    if resume:
        return None
    show_secrets(sess)
    reply = re.search(r"## Reply now: thread #(\d+) “.*?” \(([^)]+)\)", prompt)
    attached = re.search(r"\(attached: ([^,)\s]+)", prompt)
    if reply and attached and reply.group(2) != "General":
        return files_scenario(sess, int(reply.group(1)), reply.group(2), attached.group(1))
    if reply and "fake:promote" in prompt:
        return promote_scenario(sess, int(reply.group(1)))
    if reply:
        return reply_scenario(sess, int(reply.group(1)))
    if "fake:m4" in prompt:
        return m4_scenario(sess, prompt)
    if "fake:wrapup" in prompt:
        return wrapup_scenario(sess)
    if "fake:noidle" in prompt:
        return noidle_scenario(sess)
    if "- Owner (message #" in prompt:
        return mail_scenario(sess, prompt)
    return None


def fake_login() -> int:
    """`claude auth login`: print a sign-in link, read the code, write a credentials file."""
    print("Opening the browser failed. Visit this URL to sign in:\nhttps://claude.ai/oauth/authorize?code=true&client_id=fake&state=abc\n", flush=True)
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code != "good-code":
        print("\nInvalid code.", flush=True)
        return 1
    creds = Path.home() / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake-access", "refreshToken": "fake-refresh", "expiresAt": int(time.time() + 86400) * 1000}}))
    print("\nLogin successful.", flush=True)
    return 0


def mcp_call(config: str, name: str, args: dict) -> str:
    """Call one tool on the "l0w" MCP server named in a --mcp-config file, the way the CLI would."""
    import urllib.request

    server = json.loads(Path(config).read_text())["mcpServers"]["l0w"]
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **server.get("headers", {})}

    def rpc(body: dict) -> dict:
        req = urllib.request.Request(server["url"], json.dumps(body).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
        raw = next((ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")), raw)
        return json.loads(raw) if raw.strip() else {}

    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}}})
    res = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
    if "error" in res:
        return f"tool error: {res['error']}"
    parts = (res.get("result") or {}).get("content") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def chat_turn(prompt: str, config: str) -> int:
    """A 1:1 chat turn (`--mcp-config` given): reply, calling a tool when the prompt has [fake:tool NAME {json}]."""
    sid = str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": MODEL["name"], "tools": [], "mcp_servers": [{"name": "l0w", "status": "connected"}]})
    marker = re.search(r"\[fake:tool (\w+)(?: (\{.*?\}))?\]", prompt, re.S)
    text = "Hello from the fake chat."
    if marker:
        try:
            out = mcp_call(config, marker.group(1), json.loads(marker.group(2) or "{}"))
        except (OSError, ValueError, KeyError) as e:
            out = f"tool failed: {e}"
        text = f"Tool {marker.group(1)} returned: {out}"
    mid = f"msg_{uuid.uuid4().hex[:12]}"
    emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid, "model": MODEL["name"]}}, "session_id": sid})
    for i in range(0, len(text), 40):
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 40]}}, "session_id": sid})
    emit({"type": "assistant", "message": message([{"type": "text", "text": text}]), "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "num_turns": 1,
          "total_cost_usd": round(COST["usd"], 6), "usage": {"input_tokens": 3000, "output_tokens": 400, "cache_read_input_tokens": 20000}})
    return 0


def main() -> int:
    if sys.argv[1:3] == ["auth", "login"]:
        return fake_login()
    if "--version" in sys.argv:
        print("2.9.0 (fake)")
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    prompt = sys.stdin.read()
    model = arg("--model") or "sonnet"
    MODEL["name"] = f"claude-{model}-fake"
    if os.environ.get("ASSISTANT_RUN_MODE") == "reviewer":
        return reviewer_turn(prompt)
    if arg("--mcp-config") and "--strict-mcp-config" in sys.argv:
        return chat_turn(prompt, arg("--mcp-config"))
    resume = arg("--resume")
    if resume:
        path = STATE / f"{resume}.json"
        if not path.exists():
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": f"No conversation found with session ID: {resume}", "session_id": resume})
            return 1
        conv = json.loads(path.read_text())
        sid = resume
    else:
        sid = str(uuid.uuid4())
        conv = {"step": 0, "turns": 0}
        path = STATE / f"{sid}.json"
    path.write_text(json.dumps(conv))
    COST["usd"] = float(conv.get("cost_usd") or 0.0)  # Claude Code's total runs on across resumes
    CONV_FILE["path"] = path

    interrupted = INTERRUPTED
    signal.signal(signal.SIGINT, lambda *_: interrupted.__setitem__("flag", True))

    emit({"type": "system", "subtype": "init", "session_id": sid, "model": f"claude-{model}-fake", "cwd": os.getcwd(), "tools": ["Bash", "Read", "Edit", "Write"], "permissionMode": "bypassPermissions"})
    now = int(time.time())
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": now + 3 * 3600, "utilization": 0.12,
                                                          "unifiedWindows": {"five_hour": {"utilization": 0.12, "resetsAt": now + 3 * 3600}, "seven_day": {"utilization": 0.34, "resetsAt": now + 4 * 86400}}},
          "session_id": sid})
    sess = Session(sid)
    try:
        scripted = scenario(sess, prompt, resume)
    except Stopped:
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Interrupted by user", "session_id": sid, "num_turns": sess.n})
        return 130
    if scripted is not None:
        return scripted
    if resume:
        emit({"type": "assistant", "message": message([{"type": "text", "text": f"Resuming: {prompt.strip()[:200]}"}]), "session_id": sid})
    else:
        emit({"type": "assistant", "message": message([{"type": "thinking", "thinking": "Let me orient myself before starting."}]), "session_id": sid})
        out = assistant("run", "describe", "A fake session: tidy the journal and run the garden tests.")
        emit({"type": "assistant", "message": message([{"type": "tool_use", "id": "tu_describe", "name": "Bash", "input": {"command": "assistant run describe \"…\""}}]), "session_id": sid})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_describe", "content": out}]}, "session_id": sid})

    while conv["step"] < STEPS:
        if interrupted["flag"]:
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Interrupted by user", "session_id": sid, "num_turns": conv["turns"]})
            return 130
        text, tool, tool_input, result = WORK[conv["step"] % len(WORK)]
        tid = f"tu_{conv['step']}_{uuid.uuid4().hex[:6]}"
        emit({"type": "assistant", "message": message([{"type": "text", "text": text}, {"type": "tool_use", "id": tid, "name": tool, "input": tool_input}]), "session_id": sid})
        if conv["step"] % 2 == 0:
            assistant("status", text.rstrip("."))
        if conv["step"] == 2:
            assistant("note", "Journal index tidied.")
        time.sleep(DELAY)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": result}]}, "session_id": sid})
        conv["step"] += 1
        conv["turns"] += 1
        conv["cost_usd"] = COST["usd"]
        path.write_text(json.dumps(conv))

    if os.environ.get("FAKE_FAIL") == "1":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Fake failure (FAKE_FAIL=1).", "session_id": sid, "num_turns": conv["turns"]})
        return 1
    emit({"type": "assistant", "message": message([{"type": "text", "text": "IDLE: journal tidied and tests pass; nothing else worth doing."}]), "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "IDLE: journal tidied and tests pass; nothing else worth doing.",
          "session_id": sid, "num_turns": conv["turns"], "total_cost_usd": round(COST["usd"], 6), "duration_ms": int(STEPS * DELAY * 1000),
          "usage": {"input_tokens": 1200 * STEPS, "output_tokens": 300 * STEPS, "cache_read_input_tokens": 9000 * STEPS}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
