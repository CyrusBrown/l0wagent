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

Scenarios (with FAKE_CALL_ASSISTANT=1), picked from the prompt:
  Reply now           read the thread, claim it, reply, ask a question, wait for the answer
                      (delivered by the real assistant-hook after a tool call), answer, close the claim
  wakeup with mail    reply to the owner's queued messages (so control holds the run open)
  resumed for reply   control resumed the session with the owner's reply: answer it
  otherwise           the M1 session: tidy the journal
After every tool call it runs `assistant-hook` like Claude Code's PostToolUse hook does.
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

WORK = [
    ("Reading NOW.md to see what's in flight.", "Read", {"file_path": "/workspace/NOW.md"}, "# Now\n\nNothing in flight yet."),
    ("Checking the backlog.", "Bash", {"command": "cat plan/backlog.md", "description": "read the backlog"}, "# Backlog\n\n- tidy the journal"),
    ("Tidying the journal index.", "Edit", {"file_path": "/workspace/journal/README.md", "old_string": "x", "new_string": "y"}, "The file has been updated."),
    ("Running the tests for the garden script.", "Bash", {"command": "pytest -q", "description": "run tests"}, "4 passed in 0.31s"),
    ("Writing today's journal entry.", "Write", {"file_path": "/workspace/journal/2026/11-15.md", "content": "## Run\n- did things"}, "File created."),
    ("Updating NOW.md for the next session.", "Write", {"file_path": "/workspace/NOW.md", "content": "# Now\n\nJournal tidied."}, "File written."),
]


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


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
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "session_id": self.sid})

    def run(self, *args: str, show: str | None = None) -> str:
        """One Bash tool call running `assistant …`, then the PostToolUse hook."""
        if INTERRUPTED["flag"]:
            raise Stopped
        self.n += 1
        tid = f"tu_{self.n}_{uuid.uuid4().hex[:6]}"
        cmd = show or "assistant " + " ".join(a if " " not in a else repr(a) for a in args)
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}, "session_id": self.sid})
        out = assistant(*args)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": out}]}, "session_id": self.sid})
        self.hook()
        time.sleep(DELAY)
        return out

    def bash(self, command: str, output: str) -> None:
        self.n += 1
        tid = f"tu_{self.n}_{uuid.uuid4().hex[:6]}"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}, "session_id": self.sid})
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
              "total_cost_usd": 0.0, "usage": {"input_tokens": 900 * self.n, "output_tokens": 200 * self.n}})
        return 0


def show_secrets(sess: Session) -> None:
    """Prove the session got the secrets (names only in text), and echo one so the
    redaction of tool output can be checked."""
    names = [line.split()[0] for line in assistant("secret", "list").splitlines() if line and line.split()[0].isupper() and line.split()[0] in os.environ]
    if names:
        sess.say("Secrets in my environment: " + ", ".join(names))
        sess.bash(f"echo ${names[0]}", os.environ[names[0]])


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


def scenario(sess: Session, prompt: str, resume: str | None) -> int | None:
    if not CALL_ASSISTANT:
        return None
    if prompt.startswith("[control] The owner responded"):
        return resumed_scenario(sess, prompt)
    if resume:
        return None
    show_secrets(sess)
    reply = re.search(r"## Reply now: thread #(\d+)", prompt)
    if reply:
        return reply_scenario(sess, int(reply.group(1)))
    if "- Owner (message #" in prompt:
        return mail_scenario(sess, prompt)
    return None


def main() -> int:
    if "--version" in sys.argv:
        print("2.9.0 (fake)")
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    prompt = sys.stdin.read()
    model = arg("--model") or "sonnet"
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
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Resuming: {prompt.strip()[:200]}"}]}, "session_id": sid})
    else:
        emit({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Let me orient myself before starting."}]}, "session_id": sid})
        out = assistant("run", "describe", "A fake session: tidy the journal and run the garden tests.")
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu_describe", "name": "Bash", "input": {"command": "assistant run describe \"…\""}}]}, "session_id": sid})
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_describe", "content": out}]}, "session_id": sid})

    while conv["step"] < STEPS:
        if interrupted["flag"]:
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Interrupted by user", "session_id": sid, "num_turns": conv["turns"]})
            return 130
        text, tool, tool_input, result = WORK[conv["step"] % len(WORK)]
        tid = f"tu_{conv['step']}_{uuid.uuid4().hex[:6]}"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}, {"type": "tool_use", "id": tid, "name": tool, "input": tool_input}]}, "session_id": sid})
        if conv["step"] % 2 == 0:
            assistant("status", text.rstrip("."))
        if conv["step"] == 2:
            assistant("note", "Journal index tidied.")
        time.sleep(DELAY)
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": result}]}, "session_id": sid})
        conv["step"] += 1
        conv["turns"] += 1
        path.write_text(json.dumps(conv))

    if os.environ.get("FAKE_FAIL") == "1":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Fake failure (FAKE_FAIL=1).", "session_id": sid, "num_turns": conv["turns"]})
        return 1
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "IDLE: journal tidied and tests pass; nothing else worth doing."}]}, "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "IDLE: journal tidied and tests pass; nothing else worth doing.",
          "session_id": sid, "num_turns": conv["turns"], "total_cost_usd": 0.0, "duration_ms": int(STEPS * DELAY * 1000),
          "usage": {"input_tokens": 1200 * STEPS, "output_tokens": 300 * STEPS, "cache_read_input_tokens": 9000 * STEPS}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
