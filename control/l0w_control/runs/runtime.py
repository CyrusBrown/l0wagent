"""Runtime adapters: the agent CLI that drives a session (Claude Code, and opencode for opencode Go).

An adapter turns a run's settings into the runner's session request, and the runtime's
raw events into normalized ones control stores and shows. Starting, following and
stopping a session go through the runner and are the same for every runtime. Mid-run
delivery is the runtime's job (Claude Code: the PostToolUse hook in the agent image;
opencode: the runner's plugin, which runs the same hook after every tool call),
declared with a capability flag. Keep adapters thin: the CLI's headless interface changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .endings import explain_error

TOOL_TEXT_MAX = 300
RESULT_MAX = 4000


@dataclass(frozen=True)
class Capabilities:
    hooks: bool
    resume: bool
    effort: bool
    subagents: bool


@dataclass
class Item:
    """A timeline item to store."""

    kind: str
    text: str
    simple: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Parsed:
    items: list[Item] = field(default_factory=list)
    session_id: str | None = None          # the runtime's own id, for resume
    model: str | None = None
    tool_results: list[tuple[str, str, bool]] = field(default_factory=list)  # (tool_use_id, text, is_error)
    result: dict | None = None             # {"ok", "text", "turns", "cost_usd", "subtype", "usage"}
    rate_limit: dict | None = None         # the raw rate-limit info, for the usage fallback
    usage: tuple[str | None, str | None, dict] | None = None  # (message id, model, token usage) of one model message
    runner: str | None = None              # runner event: started | pre_run | exited | lost
    runner_data: dict = field(default_factory=dict)


class RuntimeAdapter:
    name: str
    capabilities: Capabilities

    def session_request(self, *, run_id: int, prompt: str, system: str, tier: str, resume_session: str | None, env: dict[str, str],
                        model: str | None = None, effort: str | None = None, mcp: list[dict] | None = None) -> dict:
        raise NotImplementedError

    def interrupted_prompt(self, reason: str) -> str:
        raise NotImplementedError

    def parse(self, event: dict) -> Parsed:
        raise NotImplementedError


def split_tier(tier: str) -> tuple[str, str | None]:
    """'sonnet/medium' → ('sonnet', 'medium')."""
    model, _, effort = tier.partition("/")
    return model.strip() or "sonnet", effort.strip() or None


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _keep_lines(text: str, n: int) -> str:
    """The final message with its line breaks (control looks for `IDLE:` on the last line);
    a long one keeps its head and its tail."""
    text = str(text).strip()
    return text if len(text) <= n else text[: n // 2] + "\n…\n" + text[-(n // 2):]


def _tool_summary(name: str, inp: dict) -> str:
    if name.lower() == "bash":
        return f"$ {_clip(inp.get('command', ''), TOOL_TEXT_MAX)}"
    for key in ("file_path", "filePath", "path", "pattern", "url", "query", "description"):
        if inp.get(key):
            return f"{name} {_clip(inp[key], TOOL_TEXT_MAX)}"
    return name


class ClaudeCode(RuntimeAdapter):
    name = "claude-code"
    label = "Claude Code"
    capabilities = Capabilities(hooks=True, resume=True, effort=True, subagents=True)

    def session_request(self, *, run_id, prompt, system, tier, resume_session, env, model=None, effort=None, mcp=None):
        """`model`/`effort` come resolved from the provider (Budgets.runtime_spec); else from the tier."""
        if model is None:
            model, effort = split_tier(tier)
        return {"run_id": run_id, "prompt": prompt, "system_append": system, "runtime": self.name, "model": model, "effort": effort,
                "resume_session": resume_session, "env": env, "mcp": list(mcp or [])}

    def interrupted_prompt(self, reason: str) -> str:
        return (f"[control] Your session was interrupted ({reason}) and has been resumed. "
                "Anything you were running in the foreground was stopped. Check where you were, then carry on.")

    def parse(self, ev: dict) -> Parsed:
        p = Parsed()
        t = ev.get("type")
        if t == "runner":
            p.runner = ev.get("event")
            p.runner_data = ev
            if p.runner == "started":
                p.items.append(Item("control", "Session started", True, {"argv": ev.get("argv_summary"), "head": ev.get("head")}))
                if ev.get("runtime") == "opencode" and ev.get("model"):
                    p.model = ev["model"]
                    p.items.append(Item("model", f"Model {ev['model']}", False, {"model": ev["model"]}))
            elif p.runner == "pre_run":
                p.items.append(Item("control", f"Pre-run report (exit {ev.get('returncode')})", False, {"output": _clip(ev.get("output") or "", RESULT_MAX)}))
            elif p.runner == "foreign_cli":
                procs = ev.get("processes") or []
                p.items.append(Item("control", "Warning: the agent started a model CLI of its own (a second session control can't meter): "
                                    + _clip("; ".join(str(x.get("cmd")) for x in procs if isinstance(x, dict)), 200), True, {"processes": procs}))
            elif p.runner == "lost":
                p.items.append(Item("error", "The agent computer restarted mid-session", True))
            elif p.runner == "exited" and ev.get("returncode") not in (0, None, 130):
                p.items.append(Item("error", explain_error(returncode=ev.get("returncode"), stderr=ev.get("stderr_tail")), True,
                                    {"stderr": _clip(ev.get("stderr_tail") or "", RESULT_MAX), "returncode": ev.get("returncode"), "session_end": "exit",
                                     "raw": f"{self.label} exited with code {ev.get('returncode')}"}))
            elif p.runner == "exited" and ev.get("error"):
                p.items.append(Item("error", f"The runner failed: {explain_error(_clip(ev['error'], 300))}", True, {"raw": _clip(ev["error"], RESULT_MAX)}))
            return p
        if t == "system" and ev.get("subtype") == "init":
            p.session_id = ev.get("session_id")
            p.model = ev.get("model")
            p.items.append(Item("model", f"Model {ev.get('model')}", False, {"model": ev.get("model"), "session_id": p.session_id}))
            return p
        if t == "assistant":
            p.session_id = ev.get("session_id")
            msg = ev.get("message") or {}
            if isinstance(msg.get("usage"), dict):
                p.usage = (msg.get("id"), msg.get("model"), msg["usage"])
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text" and block.get("text", "").strip():
                    p.items.append(Item("text", _clip(block["text"], RESULT_MAX)))
                elif bt == "thinking" and block.get("thinking", "").strip():
                    p.items.append(Item("thinking", _clip(block["thinking"], RESULT_MAX)))
                elif bt == "tool_use":
                    inp = block.get("input") or {}
                    p.items.append(Item("tool", _tool_summary(block.get("name", "tool"), inp), False,
                                        {"id": block.get("id"), "name": block.get("name"), "input": {k: _clip(v, 2000) if isinstance(v, str) else v for k, v in inp.items()}}))
            return p
        if t == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    p.tool_results.append((block.get("tool_use_id") or "", _clip(content or "", 2000), bool(block.get("is_error"))))
            return p
        if t == "rate_limit_event":
            p.rate_limit = ev.get("rate_limit_info") or {}
            info = p.rate_limit
            p.items.append(Item("usage", f"Usage window {info.get('rateLimitType', '?')}: {info.get('status', '?')}", False, {"info": info}))
            return p
        if t == "result":
            p.session_id = ev.get("session_id")
            ok = not ev.get("is_error")
            text = ev.get("result") or ev.get("subtype") or ""
            p.result = {"ok": ok, "text": _keep_lines(text, RESULT_MAX), "turns": ev.get("num_turns"), "cost_usd": ev.get("total_cost_usd"),
                        "subtype": ev.get("subtype"), "usage": ev.get("usage")}
            if ok:
                p.items.append(Item("control", f"Session finished: {_clip(text, 200)}", True, {"turns": ev.get("num_turns"), "usage": ev.get("usage")}))
            else:
                # The raw text stays in `data.raw`; the manager makes this neutral when control stopped the run.
                readable = explain_error(ev.get("result"), ev.get("subtype"))
                p.result["text"] = readable
                p.items.append(Item("error", f"Session ended with an error: {_clip(readable, 200)}", True,
                                    {"turns": ev.get("num_turns"), "usage": ev.get("usage"), "raw": _clip(text, RESULT_MAX), "subtype": ev.get("subtype"), "session_end": "result"}))
            return p
        if t == "raw":
            p.items.append(Item("error", f"Unparsed output: {_clip(ev.get('text', ''), 300)}"))
        return p


class OpenCode(ClaudeCode):
    """`opencode run --format json`: step_start, text, reasoning, tool_use (with its output),
    step_finish (tokens and cost) and error events, one JSON object per line. The runner adds
    the final result line (Claude Code's shape), so runner, result and raw lines parse as there."""

    name = "opencode"
    label = "opencode"

    def parse(self, ev: dict) -> Parsed:
        t = ev.get("type")
        if t in ("runner", "result", "raw"):
            return super().parse(ev)
        p = Parsed()
        p.session_id = ev.get("sessionID")
        part = ev.get("part") or {}
        if t == "text" and (part.get("text") or "").strip():
            p.items.append(Item("text", _clip(part["text"], RESULT_MAX)))
        elif t == "reasoning" and (part.get("text") or "").strip():
            p.items.append(Item("thinking", _clip(part["text"], RESULT_MAX)))
        elif t == "tool_use":
            state = part.get("state") or {}
            inp = state.get("input") or {}
            name = part.get("tool") or "tool"
            out = state.get("output") if state.get("status") == "completed" else (state.get("error") or state.get("output") or "")
            p.items.append(Item("tool", _tool_summary(name, inp), False,
                                {"id": part.get("callID"), "name": name, "input": {k: _clip(v, 2000) if isinstance(v, str) else v for k, v in inp.items()},
                                 "result": _clip(out or "", 2000), "is_error": state.get("status") == "error"}))
        elif t == "step_finish":
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            p.usage = (part.get("id"), None, {"input_tokens": int(tok.get("input") or 0), "output_tokens": int((tok.get("output") or 0) + (tok.get("reasoning") or 0)),
                                              "cache_read_input_tokens": int(cache.get("read") or 0), "cache_creation_input_tokens": int(cache.get("write") or 0),
                                              "cost_usd": float(part.get("cost") or 0)})
        elif t == "error":
            err = ev.get("error") or {}
            msg = (err.get("data") or {}).get("message") or err.get("name") or "error"
            p.items.append(Item("error", f"opencode: {explain_error(_clip(msg, 300))}", True, {"error": err, "raw": _clip(msg, RESULT_MAX), "session_end": "error"}))
        return p


ADAPTERS: dict[str, RuntimeAdapter] = {"claude-code": ClaudeCode(), "opencode": OpenCode()}
