#!/usr/bin/env python3
"""A stand-in for `opencode run --format json`, for development and tests.

It runs fake_claude.py (so every scenario there works on opencode too: replies, asks,
promote, budgets, the reviewer, jobs) and rewrites its stream-json into opencode's events:

  step_start · text · reasoning · tool_use (with its output) · step_finish (tokens, cost) · error

and, like opencode, writes no final result line (the runner adds one). The system prompt
comes from the `l0w` agent in $OPENCODE_CONFIG, the prompt from stdin, as with opencode.
`-m opencode-go/<model>` picks the fake's price tier from the model's name (flash/free → haiku
prices, pro → sonnet, otherwise opus), `--session ID` resumes, `--variant` is accepted.

It doesn't load the delivery plugin: fake_claude already runs `assistant-hook` after each
tool call, which is what the plugin does in a real session.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def arg(name: str) -> str | None:
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def family(model: str) -> str:
    m = model.lower()
    return "haiku" if ("flash" in m or "free" in m) else "sonnet" if "pro" in m else "opus"


def system_prompt() -> str:
    try:
        cfg = json.loads(Path(os.environ["OPENCODE_CONFIG"]).read_text())
        prompt = cfg["agent"]["l0w"]["prompt"]
    except (KeyError, OSError, json.JSONDecodeError):
        return ""
    m = re.fullmatch(r"\{file:(.+)\}", prompt.strip())
    return Path(m.group(1)).read_text() if m else prompt


def main() -> int:
    if "--version" in sys.argv:
        print("1.18.31 (fake)")
        return 0
    if sys.argv[1:2] != ["run"]:
        print("fake opencode: only `run` is faked", file=sys.stderr)
        return 2
    model = arg("-m") or arg("--model") or "opencode-go/fake"
    fam = family(model)
    resume = arg("--session")
    prompt = sys.stdin.read()
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(system_prompt())
        sys_file = f.name
    argv = [sys.executable, str(HERE / "fake_claude.py"), "-p", "--output-format", "stream-json", "--model", fam, "--append-system-prompt-file", sys_file]
    if resume:
        argv += ["--resume", resume.removeprefix("ses_")]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=False)
    signal.signal(signal.SIGINT, lambda *_: child.send_signal(signal.SIGINT))
    signal.signal(signal.SIGTERM, lambda *_: child.send_signal(signal.SIGTERM))
    assert child.stdin and child.stdout
    child.stdin.write(prompt)
    child.stdin.close()

    sid = resume or ""
    pending: dict[str, dict] = {}   # tool_use id → (name, input), until its result arrives
    open_step: dict | None = None   # the step whose tool calls are still running

    def emit(t: str, part: dict | None = None, **extra) -> None:
        ev = {"type": t, "timestamp": int(time.time() * 1000), "sessionID": sid, **extra}
        if part is not None:
            ev["part"] = {"id": f"prt_{uuid.uuid4().hex[:12]}", "sessionID": sid, **part}
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()

    def finish(step: dict, reason: str) -> None:
        u = step["usage"]
        pin, pout = PRICES[fam]
        cost = (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout + u.get("cache_read_input_tokens", 0) * pin * 0.1) / 1e6
        emit("step_finish", {"type": "step-finish", "reason": reason, "cost": round(cost, 6),
                             "tokens": {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0), "reasoning": 0,
                                        "cache": {"read": u.get("cache_read_input_tokens", 0), "write": 0}}})

    for raw in child.stdout:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if ev.get("session_id") and not sid:
            sid = "ses_" + ev["session_id"] if not str(ev["session_id"]).startswith("ses_") else ev["session_id"]
        if t == "assistant":
            if open_step:
                finish(open_step, "tool-calls")
                open_step = None
            msg = ev.get("message") or {}
            emit("step_start", {"type": "step-start"})
            tools = False
            for block in msg.get("content") or []:
                if block.get("type") == "text":
                    emit("text", {"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "thinking":
                    emit("reasoning", {"type": "reasoning", "text": block.get("thinking", "")})
                elif block.get("type") == "tool_use":
                    tools = True
                    pending[block["id"]] = {"name": block.get("name", "bash").lower(), "input": block.get("input") or {}}
            step = {"usage": msg.get("usage") or {}}
            if tools:
                open_step = step
            else:
                finish(step, "stop")
        elif t == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call = pending.pop(block["tool_use_id"])
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    inp = call["input"]
                    if "file_path" in inp:
                        inp = {**{k: v for k, v in inp.items() if k != "file_path"}, "filePath": inp["file_path"]}
                    emit("tool_use", {"type": "tool", "tool": call["name"], "callID": block["tool_use_id"],
                                      "state": {"status": "error" if block.get("is_error") else "completed", "input": inp, "output": content or ""}})
        elif t == "result" and ev.get("is_error"):
            if open_step:
                finish(open_step, "tool-calls")
                open_step = None
            text = ev.get("result") or "error"
            if text != "Interrupted by user":
                emit("error", error={"name": "UnknownError", "data": {"message": text}})
    if open_step:
        finish(open_step, "tool-calls")
    rc = child.wait()
    Path(sys_file).unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
