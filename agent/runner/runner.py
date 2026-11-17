"""Runner: a small HTTP service inside the agent container that runs agent sessions for control.

Sessions don't depend on control staying connected. Each session's output (the runtime's
stream-json, plus the runner's own events) is appended to a log file; control reads it by
byte offset and, after a restart, re-attaches from where it stopped. While control is
away nothing supervises the session, but its model access ends when control stops
renewing the egress lease (a few minutes).

  POST /sessions                  start a session (409 if one is running)
  GET  /sessions/{id}             its state: running | exited | lost
  GET  /sessions/{id}/log?offset  the log from a byte offset; follows until the session ends
  POST /sessions/{id}/stop        SIGINT (end the turn cleanly); {"force": true} kills it
  POST /commit                    git-commit /workspace (the safety net after each run)
  POST /auth/refresh              a short Claude turn so its login refreshes
  POST /auth/login                start `claude auth login`; returns the sign-in link
  POST /auth/login/code           finish it with the code the owner pasted back
  POST /consult                   one reviewer turn (read-only; no agent token or secrets)
  GET  /procs                     which of these pids are still running
  POST /selftest                  the isolation probes, run from inside the container
  GET  /health

Log lines are JSON objects. Runner events look like {"type": "runner", "event": ...}:
started, pre_run, exited (with returncode), lost (the container restarted mid-session).

Two runtimes, chosen per session: Claude Code (`claude -p --output-format stream-json`) and
opencode (`opencode run --format json`, for opencode Go). opencode's stream has no final
result line, so the runner appends one in Claude Code's shape ({"type": "result", ...}, with
the last reply, the step count and the summed cost) when an opencode session ends.

Only control can reach this (sandbox network + RUNNER_TOKEN). Everything in this container
is untrusted: control enforces limits itself and treats what it reads here as data.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

TOKEN = os.environ.get("RUNNER_TOKEN", "")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
STATE_DIR = Path(os.environ.get("RUNNER_STATE_DIR", str(Path.home() / ".runner")))
SEED = Path(os.environ.get("RUNNER_SEED", "/opt/seed"))
RUNTIME = os.environ.get("RUNNER_RUNTIME", "claude-code")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
OPENCODE = os.environ.get("OPENCODE_BIN", "opencode")
OPENCODE_PLUGIN = Path(os.environ.get("OPENCODE_PLUGIN", str(Path(__file__).parent / "opencode-plugin.js")))
SETTINGS = os.environ.get("CLAUDE_SETTINGS", "/opt/runner/claude-settings.json")
PRE_RUN = WORKSPACE / "system" / "pre-run.sh"
PRE_RUN_TIMEOUT = 120
PRE_RUN_MAX_CHARS = 8000
FOLLOW_POLL_S = 0.2
# While a session's log is quiet, the stream sends this every HEARTBEAT_S. It's a line that
# can't occur in the log (JSON lines only), and control doesn't count it in its byte offset.
HEARTBEAT = b"\x1e\n"
HEARTBEAT_S = 10.0
KEEP_LOGS_DAYS = 30

app = FastAPI(docs_url=None, redoc_url=None)
_lock = asyncio.Lock()
_current: dict | None = None  # {"id", "proc", "task"}


# S23: only control's address may call the runner. Entries are IPs, CIDRs or host names (compose
# uses `control`, whose address Docker assigns); unset means anyone (dev_stack on localhost).
ALLOWED_FROM = [x.strip() for x in os.environ.get("RUNNER_ALLOWED_FROM", "").split(",") if x.strip()]
_resolved: dict[str, tuple[float, set]] = {}


def _allowed_nets() -> list:
    nets = []
    for entry in ALLOWED_FROM:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        at, found = _resolved.get(entry, (0.0, set()))
        if time.monotonic() - at > 30:
            try:
                found = {ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(entry, None)}
            except OSError:
                pass  # keep the last answer
            _resolved[entry] = (time.monotonic(), found)
        nets.extend(ipaddress.ip_network(a) for a in found)
    return nets


def source_allowed(host: str | None) -> bool:
    if not ALLOWED_FROM:
        return True
    try:
        addr = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr.version == n.version and addr in n for n in _allowed_nets())


@app.middleware("http")
async def only_from_control(request: Request, call_next):
    if not source_allowed(request.client.host if request.client else None):
        return JSONResponse({"detail": "not control's address"}, status_code=403)
    return await call_next(request)


def _auth(token: str | None) -> None:
    if not TOKEN or not token or not secrets.compare_digest(token, TOKEN):
        raise HTTPException(401, "bad runner token")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=WORKSPACE, capture_output=True, text=True, timeout=120)


# ── session files ─────────────────────────────────────────────────────────
def _log(sid: str) -> Path:
    return STATE_DIR / "sessions" / f"{sid}.ndjson"


def _meta_path(sid: str) -> Path:
    return STATE_DIR / "sessions" / f"{sid}.json"


def _meta(sid: str) -> dict | None:
    try:
        return json.loads(_meta_path(sid).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_meta(sid: str, **fields) -> dict:
    meta = {**(_meta(sid) or {}), **fields}
    tmp = _meta_path(sid).with_suffix(".tmp")
    tmp.write_text(json.dumps(meta))
    tmp.replace(_meta_path(sid))
    return meta


def _append(f, obj: dict) -> None:
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()


def recover() -> None:
    """At startup: sessions still marked running were cut off by a container restart."""
    (STATE_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - KEEP_LOGS_DAYS * 86400
    for meta_file in (STATE_DIR / "sessions").glob("*.json"):
        sid = meta_file.stem
        meta = _meta(sid) or {}
        if meta.get("state") == "running":
            with open(_log(sid), "ab") as f:
                _append(f, {"type": "runner", "event": "lost", "reason": "the agent computer restarted mid-session"})
            _save_meta(sid, state="lost", ended_at=time.time())
        elif meta.get("ended_at", time.time()) < cutoff:
            meta_file.unlink(missing_ok=True)
            _log(sid).unlink(missing_ok=True)


def seed_workspace() -> None:
    """First boot: copy the seed workspace in and start its git history."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if SEED.is_dir() and not any(p for p in WORKSPACE.iterdir() if p.name != "lost+found"):
        shutil.copytree(SEED, WORKSPACE, dirs_exist_ok=True)
    if not (WORKSPACE / ".git").exists():
        _git("init", "-q", "-b", "main")
        _git("add", "-A")
        _git("commit", "-q", "--allow-empty", "-m", "Seed workspace")


# ── running a session ─────────────────────────────────────────────────────
class SessionRequest(BaseModel):
    run_id: int
    prompt: str
    system_append: str = ""
    runtime: str = "claude-code"   # claude-code | opencode
    model: str = "sonnet"          # opencode: provider/model, e.g. opencode-go/glm-5.3
    effort: str | None = None
    fallback_model: str | None = None
    resume_session: str | None = None
    env: dict[str, str] = {}
    pre_run: bool = True
    mcp: list[dict] = []


MCP_NAME = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def mcp_servers(req_mcp: list[dict], control_url: str | None = None, token: str | None = None) -> dict[str, dict]:
    """The brokered integrations as remote MCP servers on control's agent API."""
    base = (control_url if control_url is not None else os.environ.get("CONTROL_URL", "http://control:8000")).rstrip("/")
    token = token if token is not None else os.environ.get("AGENT_API_TOKEN", "")
    out = {}
    for m in req_mcp:
        name = str(m.get("name", ""))
        if MCP_NAME.match(name):
            out[name] = {"url": f"{base}/api/agent/mcp/{name}", "headers": {"X-Agent-Token": token}}
    return out


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def claude_mcp_config(servers: dict[str, dict]) -> dict:
    return {"mcpServers": {n: {"type": "http", **v} for n, v in servers.items()}}


def claude_argv(req: SessionRequest, system_file: Path) -> list[str]:
    argv = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
            "--model", req.model, "--append-system-prompt-file", str(system_file)]
    if Path(SETTINGS).is_file():
        argv += ["--settings", SETTINGS]
    if req.effort:
        argv += ["--effort", req.effort]
    if req.fallback_model:
        argv += ["--fallback-model", req.fallback_model]
    if req.resume_session:
        argv += ["--resume", req.resume_session]
    servers = mcp_servers(req.mcp)
    if servers:
        mcp_file = system_file.with_name("mcp.json")
        write_private(mcp_file, json.dumps(claude_mcp_config(servers)))
        argv += ["--mcp-config", str(mcp_file)]
    return argv


def opencode_config(system_file: Path, reviewer: bool = False, mcp: dict[str, dict] | None = None) -> dict:
    """opencode's config for one session: our system prompt as the `l0w` agent, the delivery
    plugin (not for the reviewer), no prompts for permissions, no sharing or self-updates."""
    # No AGENTS.md is seeded: opencode 1.18 falls back to CLAUDE.md when there's none (an AGENTS.md
    # would replace it) and finds skills in .claude/skills/**/SKILL.md, so the seed works as is.
    permission = {"*": "allow", "external_directory": "allow"}
    if reviewer:
        permission.update(edit="deny")
    cfg: dict = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled", "permission": permission,
                 "agent": {"l0w": {"mode": "primary", "prompt": "{file:" + str(system_file) + "}"}}}
    if not reviewer and OPENCODE_PLUGIN.is_file():
        cfg["plugin"] = [f"file://{OPENCODE_PLUGIN}"]
    if mcp and not reviewer:
        cfg["mcp"] = {n: {"type": "remote", **v, "enabled": True} for n, v in mcp.items()}
    return cfg


def opencode_argv(model: str, effort: str | None, resume: str | None) -> list[str]:
    argv = [OPENCODE, "run", "--format", "json", "--auto", "-m", model, "--agent", "l0w"]
    if effort:
        argv += ["--variant", effort]
    if resume:
        argv += ["--session", resume]
    return argv


class OpencodeResult:
    """Builds the result line opencode doesn't write: its last reply, steps, cost and tokens."""

    def __init__(self, resume: str | None):
        self.session_id, self.steps, self.cost = resume, 0, 0.0
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.step_text: list[str] = []
        self.last_text, self.error = "", None

    def see(self, ev: dict) -> None:
        self.session_id = ev.get("sessionID") or self.session_id
        t, part = ev.get("type"), ev.get("part") or {}
        if t == "step_start":
            self.step_text = []
        elif t == "text" and (part.get("text") or "").strip():
            self.step_text.append(part["text"].strip())
            self.last_text = "\n\n".join(self.step_text)
        elif t == "step_finish":
            self.steps += 1
            self.cost += float(part.get("cost") or 0)
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            for k, v in (("input_tokens", tok.get("input")), ("output_tokens", (tok.get("output") or 0) + (tok.get("reasoning") or 0)),
                         ("cache_read_input_tokens", cache.get("read")), ("cache_creation_input_tokens", cache.get("write"))):
                self.usage[k] += int(v or 0)
        elif t == "error":
            err = ev.get("error") or {}
            self.error = (err.get("data") or {}).get("message") or err.get("name") or "error"

    def line(self, rc: int | None) -> dict:
        interrupted = rc in (130, -2) and not self.error
        is_error = bool(self.error) or interrupted or rc not in (0, None)
        text = self.error or ("Interrupted" if interrupted else self.last_text) or ("" if not is_error else f"opencode exited with code {rc}")
        return {"type": "result", "runtime": "opencode", "subtype": "error_during_execution" if is_error else "success", "is_error": is_error,
                "result": text, "session_id": self.session_id, "num_turns": self.steps, "total_cost_usd": round(self.cost, 6), "usage": self.usage}


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 20) -> None:
    """SIGINT, then SIGTERM, then SIGKILL the whole process group."""
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
        except TimeoutError:
            continue


async def _pre_run(env: dict) -> tuple[str, int | None]:
    """The agent's own pre-session script; its output goes into the prompt."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(PRE_RUN), cwd=WORKSPACE, env=env, start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return f"could not start: {e}", None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PRE_RUN_TIMEOUT)
    except TimeoutError:
        await _terminate(proc, grace=2)
        return f"timed out after {PRE_RUN_TIMEOUT}s", None
    text = out.decode(errors="replace").strip()
    if len(text) > PRE_RUN_MAX_CHARS:
        text = text[:PRE_RUN_MAX_CHARS] + f"\n… [{len(text) - PRE_RUN_MAX_CHARS} more chars cut; keep pre-run.sh output short]"
    return text.replace("~~~", "~ ~ ~"), proc.returncode



# ── S23: model CLIs the session didn't start through control ───────────
CLI_NAMES = {"claude", "opencode"}
LAUNCHERS = {"node", "bun", "npm", "npx"}
WATCH_EVERY_S = 15.0
_own_procs: set[int] = set()   # reviewer / login processes the runner started itself


def _proc_table() -> dict[int, tuple[int, list[str]]]:
    """pid -> (ppid, argv) for every process we can see (Linux /proc; empty elsewhere)."""
    table = {}
    try:
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return table
    for pid in pids:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        table[pid] = (ppid, [a.decode(errors="replace") for a in raw.split(b"\0") if a])
    return table


def _is_cli(argv: list[str]) -> bool:
    if not argv or any(a in ("--version", "-v") for a in argv[1:3]):
        return False
    first = os.path.basename(argv[0])
    if first in CLI_NAMES:
        return True
    # node/bun running the CLI's script
    return first in LAUNCHERS and len(argv) > 1 and (os.path.basename(argv[1]) in CLI_NAMES or "claude-code" in argv[1] or "opencode-ai" in argv[1])


def foreign_cli(root_pids: set[int]) -> list[dict]:
    """Model-CLI processes that aren't the session's own: a CLI whose chain of parents up to a
    root pid (the session, the reviewer, a login) passes through anything but a CLI or its
    launcher, e.g. `claude -p` run from a tool call's shell, or one started by cron or tmux."""
    table = _proc_table()
    found = []
    for pid, (_, argv) in table.items():
        if pid in root_pids or pid == os.getpid() or not _is_cli(argv):
            continue
        cur, own = pid, False
        for _ in range(64):
            ppid = table.get(cur, (0, []))[0]
            if ppid in root_pids:
                own = True
                break
            if ppid not in table or not (_is_cli(table[ppid][1]) or os.path.basename((table[ppid][1] or [""])[0]) in LAUNCHERS):
                break
            cur = ppid
        if not own:
            found.append({"pid": pid, "cmd": " ".join(argv)[:200]})
    return found


async def _watch_cli(sid: str, f, proc_getter) -> None:
    """While the session runs, note once on its log if another model CLI is running."""
    while True:
        await asyncio.sleep(WATCH_EVERY_S)
        proc = proc_getter()
        if proc is None or proc.returncode is not None:
            continue
        roots = {proc.pid} | _own_procs
        found = await asyncio.to_thread(foreign_cli, roots)
        if found:
            _append(f, {"type": "runner", "event": "foreign_cli", "processes": found[:5]})
            return


async def _run_session(sid: str, req: SessionRequest) -> None:
    global _current
    tmp = tempfile.TemporaryDirectory(prefix="session-")
    system_file = Path(tmp.name) / "system.md"
    system_file.write_text(req.system_append)
    env = {**os.environ, **req.env, "ASSISTANT_RUN_ID": str(req.run_id), "ASSISTANT_SESSION": sid}
    env.pop("RUNNER_TOKEN", None)
    proc = None
    watch_task = None
    with open(_log(sid), "ab") as f:
        try:
            prompt = req.prompt
            if req.pre_run and not req.resume_session and PRE_RUN.is_file():
                report, rc = await _pre_run(env)
                _append(f, {"type": "runner", "event": "pre_run", "returncode": rc, "output": report})
                prompt += f"\n\n## Pre-run report (output of your system/pre-run.sh, exit {rc})\n\n~~~\n{report}\n~~~\n"
            opencode = req.runtime == "opencode"
            if opencode:
                cfg_file = Path(tmp.name) / "opencode.json"
                write_private(cfg_file, json.dumps(opencode_config(system_file, mcp=mcp_servers(req.mcp))))
                env["OPENCODE_CONFIG"] = str(cfg_file)
                argv = opencode_argv(req.model, req.effort, req.resume_session)
            else:
                argv = claude_argv(req, system_file)
            oc = OpencodeResult(req.resume_session) if opencode else None
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=WORKSPACE, env=env, start_new_session=True, limit=32 * 1024 * 1024,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            assert _current is not None
            _current["proc"] = proc
            head = (await asyncio.to_thread(_git, "rev-parse", "HEAD")).stdout.strip() or None
            _append(f, {"type": "runner", "event": "started", "pid": proc.pid, "head": head, "runtime": req.runtime, "model": req.model,
                        "argv_summary": [a for a in argv[1:] if not a.startswith(tmp.name)]})
            stderr_tail: list[str] = []

            async def read_stderr():
                assert proc and proc.stderr
                async for raw in proc.stderr:
                    stderr_tail.append(raw.decode(errors="replace"))
                    del stderr_tail[:-60]

            err_task = asyncio.create_task(read_stderr())
            watch_task = asyncio.create_task(_watch_cli(sid, f, lambda: proc))
            assert proc.stdin and proc.stdout
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    f.write(line + b"\n")
                    f.flush()
                    if oc is not None and isinstance(ev, dict):
                        oc.see(ev)
                except json.JSONDecodeError:
                    _append(f, {"type": "raw", "text": line.decode(errors="replace")[:4000]})
            rc = await proc.wait()
            watch_task.cancel()
            if oc is not None:
                _append(f, oc.line(rc))
            try:
                await asyncio.wait_for(err_task, 5)
            except TimeoutError:
                err_task.cancel()
            _append(f, {"type": "runner", "event": "exited", "returncode": rc, "stderr_tail": "".join(stderr_tail)[-4000:]})
            _save_meta(sid, state="exited", returncode=rc, ended_at=time.time())
        except Exception as e:  # noqa: BLE001 - the session must always end with a record
            _append(f, {"type": "runner", "event": "exited", "returncode": None, "error": f"{type(e).__name__}: {e}"})
            _save_meta(sid, state="exited", returncode=None, ended_at=time.time())
        finally:
            if watch_task is not None:
                watch_task.cancel()
            with anyio.CancelScope(shield=True):
                if proc is not None:
                    await _terminate(proc, grace=2)
                tmp.cleanup()
                _current = None


@app.post("/sessions")
async def start(req: SessionRequest, x_runner_token: str | None = Header(None)):
    global _current
    _auth(x_runner_token)
    async with _lock:
        if _current is not None:
            raise HTTPException(409, f"session {_current['id']} is still running")
        sid = f"r{req.run_id}-{int(time.time())}-{secrets.token_hex(3)}"
        _log(sid).touch()
        _save_meta(sid, id=sid, run_id=req.run_id, state="running", started_at=time.time(), resume=req.resume_session)
        _current = {"id": sid, "proc": None}
        _current["task"] = asyncio.create_task(_run_session(sid, req))
    return {"id": sid}


def _state(sid: str) -> dict:
    meta = _meta(sid)
    if meta is None:
        raise HTTPException(404, "no such session")
    try:
        size = _log(sid).stat().st_size
    except FileNotFoundError:
        size = 0
    return {"id": sid, "run_id": meta.get("run_id"), "state": meta.get("state"), "returncode": meta.get("returncode"), "size": size}


@app.get("/sessions/{sid}")
async def session(sid: str, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    return _state(sid)


@app.get("/sessions/{sid}/log")
async def session_log(sid: str, offset: int = 0, follow: bool = True, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    _state(sid)

    async def gen():
        quiet = 0.0
        with open(_log(sid), "rb") as f:
            f.seek(offset)
            while True:
                chunk = f.read(65536)
                if chunk:
                    quiet = 0.0
                    yield chunk
                    continue
                if not follow or (_meta(sid) or {}).get("state") != "running":
                    rest = f.read()  # anything written between the read and the state check
                    if rest:
                        yield rest
                    return
                await asyncio.sleep(FOLLOW_POLL_S)
                quiet += FOLLOW_POLL_S
                if quiet >= HEARTBEAT_S:  # a quiet session isn't a lost one: tell control we're here
                    quiet = 0.0
                    yield HEARTBEAT

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Session-State": _state(sid)["state"] or ""})


class StopRequest(BaseModel):
    force: bool = False


@app.post("/sessions/{sid}/stop")
async def stop(sid: str, req: StopRequest | None = None, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    cur = _current
    if not cur or cur["id"] != sid or cur.get("proc") is None or cur["proc"].returncode is not None:
        return {"stopping": False}
    try:
        os.killpg(cur["proc"].pid, signal.SIGKILL if req and req.force else signal.SIGINT)
    except ProcessLookupError:
        return {"stopping": False}
    return {"stopping": True}


class CommitRequest(BaseModel):
    message: str
    since: str | None = None  # the workspace HEAD when the run started, for the run's file list


@app.post("/commit")
async def commit(req: CommitRequest, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)

    def do():
        _git("add", "-A")
        status = "nothing to commit"
        if _git("status", "--porcelain").stdout.strip():
            r = _git("commit", "-q", "-m", req.message)
            if r.returncode != 0:
                return {"status": "error", "error": (r.stderr or r.stdout)[-1000:]}
            status = "committed"
        numstat = []
        if req.since and _git("cat-file", "-e", f"{req.since}^{{commit}}").returncode == 0:
            for line in _git("diff", "--numstat", req.since, "HEAD").stdout.splitlines()[:200]:
                added, removed, path = (line.split("\t", 2) + ["", "", ""])[:3]
                numstat.append({"path": path, "added": int(added) if added.isdigit() else None, "removed": int(removed) if removed.isdigit() else None})
        return {"status": status, "commit": _git("rev-parse", "--short", "HEAD").stdout.strip(), "files": numstat}

    return await asyncio.to_thread(do)


@app.post("/auth/refresh")
async def auth_refresh(x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    if _current is not None:
        raise HTTPException(409, "a session is running")
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", "Reply with just: ok", "--model", "haiku", "--max-turns", "1",
        "--output-format", "json", "--no-session-persistence", "--tools", "",
        cwd="/tmp", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 150)
    except TimeoutError:
        proc.kill()
        raise HTTPException(504, "refresh timed out") from None
    return {"returncode": proc.returncode, "stdout": out.decode(errors="replace")[-1500:], "stderr": err.decode(errors="replace")[-1500:]}


# ── logging in to Claude through the server (the setup wizard) ──────────
# `claude auth login` prints a sign-in link and waits for the code the owner pastes back. The
# runner runs it in a pseudo-terminal, hands the link to control (which shows it in the UI),
# and writes the code in. Control holds an egress lease meanwhile.
_login: dict = {}
URL_RE = __import__("re").compile(r"https://[^\s\x1b\x07]+")


async def _login_read(timeout: float) -> str:
    """Read what the login process printed (until it goes quiet or exits)."""
    import select

    fd, out, deadline = _login["fd"], b"", time.time() + timeout
    while time.time() < deadline:
        r, _, _ = await asyncio.to_thread(select.select, [fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        elif out and (URL_RE.search(out.decode(errors="replace")) or _login["proc"].returncode is not None):
            break
    return out.decode(errors="replace")


@app.post("/auth/login")
async def auth_login(x_runner_token: str | None = Header(None)):
    import pty

    _auth(x_runner_token)
    if _current is not None:
        raise HTTPException(409, "a session is running")
    if _login.get("proc") and _login["proc"].returncode is None:
        _login["proc"].kill()
    master, slave = pty.openpty()
    proc = await asyncio.create_subprocess_exec(CLAUDE, "auth", "login", stdin=slave, stdout=slave, stderr=slave, cwd="/tmp", start_new_session=True,
                                                env={**os.environ, "BROWSER": "/bin/false", "TERM": "dumb"})
    os.close(slave)
    _login.update(proc=proc, fd=master, output="")
    text = await _login_read(30)
    _login["output"] = text
    m = URL_RE.search(text)
    if not m:
        proc.kill()
        raise HTTPException(502, f"no sign-in link from `claude auth login`: {text[-500:]}")
    return {"url": m.group(0).rstrip(".)"), "output": text[-1000:]}


class LoginCode(BaseModel):
    code: str


@app.post("/auth/login/code")
async def auth_login_code(req: LoginCode, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    proc = _login.get("proc")
    if not proc or proc.returncode is not None:
        raise HTTPException(409, "no login in progress: start again")
    os.write(_login["fd"], req.code.strip().encode() + b"\r")
    text = await _login_read(60)
    try:
        await asyncio.wait_for(proc.wait(), 30)
    except TimeoutError:
        proc.kill()
    os.close(_login["fd"])
    _login.clear()
    creds = Path.home() / ".claude" / ".credentials.json"
    return {"ok": proc.returncode == 0 and creds.exists(), "returncode": proc.returncode, "output": text[-1500:]}


class SelfTestRequest(BaseModel):
    targets: dict = {}


@app.post("/selftest")
async def selftest(req: SelfTestRequest, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    sys.path.insert(0, str(Path(__file__).parent))
    import selftest as probes  # noqa: PLC0415

    return {"checks": await asyncio.to_thread(probes.run, req.targets)}


# ── the reviewer: one turn of a second, read-only session ───────────────
# S24: consults run as a second unix user (`reviewer`) with its own home and a scrubbed environment.
# The agent's home is 0700, so the reviewer can't read the agent's files or its processes' environment;
# /workspace and /work are read as "other" (the entrypoint keeps their top directories 0755).
REVIEWER_USER = os.environ.get("RUNNER_REVIEWER_USER", "reviewer")
# Environment the reviewer's CLI needs; nothing else is passed (no agent token, runner token, secrets).
REVIEWER_ENV_KEEP = ("PATH", "LANG", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                     "NODE_USE_ENV_PROXY", "ENABLE_CLAUDEAI_MCP_SERVERS", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                     "CLAUDE_CODE_OAUTH_TOKEN")
_reviewer_logged = False


def reviewer_account() -> pwd.struct_passwd | None:
    """The reviewer's account if consults can run as it from here, else None (logged once)."""
    global _reviewer_logged
    why = None
    pw = None
    if not REVIEWER_USER:
        why = "disabled"
    else:
        try:
            pw = pwd.getpwnam(REVIEWER_USER)
        except KeyError:
            why = f"no unix user {REVIEWER_USER!r}"
        if pw and os.geteuid() != 0 and not shutil.which("sudo"):
            why, pw = "not root and no sudo", None
    if why and not _reviewer_logged:
        print(f"[runner] reviewer consults run as the agent's own user ({why})", file=sys.stderr, flush=True)
    _reviewer_logged = True
    return pw


def _as_reviewer(pw: pwd.struct_passwd, argv: list[str], env_keys: list[str] | None = None) -> list[str]:
    """argv wrapped to run as the reviewer: setpriv when we're root, sudo otherwise."""
    if os.geteuid() == 0:
        return ["setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}", "--clear-groups", *argv]
    keep = f"--preserve-env={','.join(env_keys)}" if env_keys else "--preserve-env"
    return ["sudo", "-n", "-u", pw.pw_name, "-H", keep, "--", *argv]


def _reviewer_env(pw: pwd.struct_passwd, extra: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in REVIEWER_ENV_KEEP if k in os.environ}
    env.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    env.update({k: v for k, v in extra.items() if k in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "ASSISTANT_RUN_ID", "ASSISTANT_RUN_MODE")})
    return env


def _reviewer_creds_in(pw: pwd.struct_passwd) -> bytes | None:
    """Copy just the login file into the reviewer's home (0600, owned by it). The agent's home
    stays closed to the reviewer, so the credential has to be handed over, not shared."""
    src = Path.home() / ".claude" / ".credentials.json"
    try:
        data = src.read_bytes()
    except OSError:
        return None
    script = 'umask 077; mkdir -p "$HOME/.claude" && cat > "$HOME/.claude/.credentials.json"'
    subprocess.run(_as_reviewer(pw, ["sh", "-c", script]), input=data, check=False, timeout=30, capture_output=True)
    return data


def _reviewer_creds_back(pw: pwd.struct_passwd, before: bytes | None) -> None:
    """The CLI may refresh its login during a consult; hand the newer file back so the agent's
    copy isn't left holding a rotated-out refresh token."""
    if before is None:
        return
    r = subprocess.run(_as_reviewer(pw, ["cat", f"{pw.pw_dir}/.claude/.credentials.json"]), capture_output=True, timeout=30)
    after = r.stdout
    if r.returncode != 0 or not after or after == before:
        return
    try:
        json.loads(after)
    except ValueError:
        return
    dst = Path.home() / ".claude" / ".credentials.json"
    tmp = dst.with_suffix(".tmp")
    write_private(tmp, after.decode())
    tmp.replace(dst)


REVIEWER_HOME = Path(os.environ.get("REVIEWER_HOME", str(STATE_DIR / "reviewer")))
REVIEWER_DENY = "Write Edit MultiEdit NotebookEdit"
CONSULT_TIMEOUT = 540
_consult_lock = asyncio.Lock()


class ConsultRequest(BaseModel):
    run_id: int
    prompt: str
    system_append: str = ""
    runtime: str = "claude-code"
    model: str = "opus"
    effort: str | None = None
    resume_session: str | None = None
    env: dict[str, str] = {}   # the reviewer's provider key (opencode); never the agent's secrets


@app.post("/consult")
async def consult(req: ConsultRequest, x_runner_token: str | None = Header(None)):
    """One reviewer turn: a second `claude -p` next to the session, with no hook, no agent
    token, no secrets, and no file-writing tools; /workspace is added for reading. Blocks
    until the turn ends and returns its final text, session id and cost."""
    _auth(x_runner_token)
    if _consult_lock.locked():
        raise HTTPException(409, "a reviewer turn is already in progress")
    async with _consult_lock:
        pw = reviewer_account()
        home = Path(pw.pw_dir) if pw else REVIEWER_HOME
        home.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(prefix="consult-")
        sys_file = Path(tmp.name) / "system.md"
        sys_file.write_text(req.system_append)
        if pw:
            os.chmod(tmp.name, 0o755)  # the reviewer reads its prompt and config from here
            os.chmod(sys_file, 0o644)
            env = _reviewer_env(pw, {**req.env, "ASSISTANT_RUN_ID": str(req.run_id), "ASSISTANT_RUN_MODE": "reviewer"})
        else:
            env = {k: v for k, v in os.environ.items() if k not in ("RUNNER_TOKEN", "AGENT_API_TOKEN", "CONTROL_URL")}
            env.update({k: v for k, v in req.env.items() if k in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY")})
            env.update(ASSISTANT_RUN_ID=str(req.run_id), ASSISTANT_RUN_MODE="reviewer")
        oc = OpencodeResult(req.resume_session) if req.runtime == "opencode" else None
        if oc is not None:
            cfg_file = Path(tmp.name) / "opencode.json"
            cfg_file.write_text(json.dumps(opencode_config(sys_file, reviewer=True)))
            os.chmod(cfg_file, 0o644)
            env["OPENCODE_CONFIG"] = str(cfg_file)
            argv = opencode_argv(req.model, req.effort, req.resume_session)
        else:
            argv = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions", "--model", req.model,
                    "--append-system-prompt-file", str(sys_file), "--add-dir", str(WORKSPACE), "--disallowedTools", REVIEWER_DENY]
            if req.effort:
                argv += ["--effort", req.effort]
            if req.resume_session:
                argv += ["--resume", req.resume_session]
        creds = await asyncio.to_thread(_reviewer_creds_in, pw) if pw and req.runtime != "opencode" else None
        if pw:
            argv = _as_reviewer(pw, argv, list(env))
        proc = await asyncio.create_subprocess_exec(*argv, cwd=home, env=env, start_new_session=True, limit=32 * 1024 * 1024,
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _own_procs.add(proc.pid)
        out = {"ok": False, "text": "", "session_id": req.resume_session, "cost_usd": None, "usage": None, "model": None, "messages": []}
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(req.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            async def read():
                async for raw in proc.stdout:  # type: ignore[union-attr]
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if oc is not None:
                        oc.see(ev)
                        part = ev.get("part") or {}
                        if ev.get("type") == "step_finish":
                            out["messages"].append({"id": part.get("id"), "model": req.model, "usage": {"cost_usd": float(part.get("cost") or 0)}})
                        continue
                    if ev.get("session_id"):
                        out["session_id"] = ev["session_id"]
                    if ev.get("type") == "assistant":
                        msg = ev.get("message") or {}
                        out["model"] = msg.get("model") or out["model"]
                        if msg.get("usage"):
                            out["messages"].append({"id": msg.get("id"), "model": msg.get("model"), "usage": msg["usage"]})
                    elif ev.get("type") == "result":
                        out.update(ok=not ev.get("is_error"), text=ev.get("result") or "", cost_usd=ev.get("total_cost_usd"), usage=ev.get("usage"))
                rc = await proc.wait()
                if oc is not None:
                    r = oc.line(rc)
                    out.update(ok=not r["is_error"], text=r["result"] or "", cost_usd=r["total_cost_usd"], usage=r["usage"],
                               session_id=r["session_id"], model=req.model)

            await asyncio.wait_for(read(), CONSULT_TIMEOUT)
        except TimeoutError:
            out["text"] = f"(the reviewer turn timed out after {CONSULT_TIMEOUT}s)"
        finally:
            with anyio.CancelScope(shield=True):
                await _terminate(proc, grace=2)
                if pw:  # a SIGKILL to sudo isn't relayed; make sure nothing of the reviewer's is left
                    await asyncio.to_thread(subprocess.run, _as_reviewer(pw, ["pkill", "-KILL", "-u", pw.pw_name]), capture_output=True, timeout=10, check=False)
                    await asyncio.to_thread(_reviewer_creds_back, pw, creds)
                _own_procs.discard(proc.pid)
                tmp.cleanup()
        return out


@app.get("/procs")
async def procs(pids: str = "", x_runner_token: str | None = Header(None)):
    """Which of these pids are still running (the job registry's liveness check)."""
    _auth(x_runner_token)
    alive = []
    for p in pids.split(","):
        if p.strip().isdigit():
            try:
                os.kill(int(p), 0)
                alive.append(int(p))
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.append(int(p))
    return {"alive": alive}


@app.get("/health")
async def health(x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    creds = Path.home() / ".claude" / ".credentials.json"
    try:
        version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        version = f"unavailable: {e}"
    try:
        oc_version = subprocess.run([OPENCODE, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        oc_version = None
    return {"ok": True, "busy": _current is not None, "session": _current["id"] if _current else None, "runtime": RUNTIME,
            "runtime_version": version, "opencode_version": oc_version, "logged_in": creds.exists() or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("RUNNER_TOKEN is required")
    recover()
    seed_workspace()
    uvicorn.run(app, host=os.environ.get("RUNNER_HOST", "0.0.0.0"), port=int(os.environ.get("RUNNER_PORT", "8765")), log_level="warning")
