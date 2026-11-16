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
  POST /auth/refresh              one tiny call so Claude Code refreshes its login
  POST /selftest                  the isolation probes, run from inside the container
  GET  /health

Log lines are JSON objects. Runner events look like {"type": "runner", "event": ...}:
started, pre_run, exited (with returncode), lost (the container restarted mid-session).

Only control can reach this (sandbox network + RUNNER_TOKEN). Everything in this container
is untrusted: control enforces limits itself and treats what it reads here as data.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

TOKEN = os.environ.get("RUNNER_TOKEN", "")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
STATE_DIR = Path(os.environ.get("RUNNER_STATE_DIR", str(Path.home() / ".runner")))
SEED = Path(os.environ.get("RUNNER_SEED", "/opt/seed"))
RUNTIME = os.environ.get("RUNNER_RUNTIME", "claude-code")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
SETTINGS = os.environ.get("CLAUDE_SETTINGS", "/opt/runner/claude-settings.json")
PRE_RUN = WORKSPACE / "system" / "pre-run.sh"
PRE_RUN_TIMEOUT = 120
PRE_RUN_MAX_CHARS = 8000
FOLLOW_POLL_S = 0.2
KEEP_LOGS_DAYS = 30

app = FastAPI(docs_url=None, redoc_url=None)
_lock = asyncio.Lock()
_current: dict | None = None  # {"id", "proc", "task"}


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
    model: str = "sonnet"
    effort: str | None = None
    fallback_model: str | None = None
    resume_session: str | None = None
    env: dict[str, str] = {}
    pre_run: bool = True


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
    return argv


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


async def _run_session(sid: str, req: SessionRequest) -> None:
    global _current
    tmp = tempfile.TemporaryDirectory(prefix="session-")
    system_file = Path(tmp.name) / "system.md"
    system_file.write_text(req.system_append)
    env = {**os.environ, **req.env, "ASSISTANT_RUN_ID": str(req.run_id), "ASSISTANT_SESSION": sid}
    env.pop("RUNNER_TOKEN", None)
    proc = None
    with open(_log(sid), "ab") as f:
        try:
            prompt = req.prompt
            if req.pre_run and not req.resume_session and PRE_RUN.is_file():
                report, rc = await _pre_run(env)
                _append(f, {"type": "runner", "event": "pre_run", "returncode": rc, "output": report})
                prompt += f"\n\n## Pre-run report (output of your system/pre-run.sh, exit {rc})\n\n~~~\n{report}\n~~~\n"
            argv = claude_argv(req, system_file)
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=WORKSPACE, env=env, start_new_session=True, limit=32 * 1024 * 1024,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            assert _current is not None
            _current["proc"] = proc
            head = (await asyncio.to_thread(_git, "rev-parse", "HEAD")).stdout.strip() or None
            _append(f, {"type": "runner", "event": "started", "pid": proc.pid, "head": head, "runtime": RUNTIME,
                        "argv_summary": [a for a in argv[1:] if not a.startswith(tmp.name)]})
            stderr_tail: list[str] = []

            async def read_stderr():
                assert proc and proc.stderr
                async for raw in proc.stderr:
                    stderr_tail.append(raw.decode(errors="replace"))
                    del stderr_tail[:-60]

            err_task = asyncio.create_task(read_stderr())
            assert proc.stdin and proc.stdout
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    f.write(line + b"\n")
                    f.flush()
                except json.JSONDecodeError:
                    _append(f, {"type": "raw", "text": line.decode(errors="replace")[:4000]})
            rc = await proc.wait()
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
        with open(_log(sid), "rb") as f:
            f.seek(offset)
            while True:
                chunk = f.read(65536)
                if chunk:
                    yield chunk
                    continue
                if not follow or (_meta(sid) or {}).get("state") != "running":
                    rest = f.read()  # anything written between the read and the state check
                    if rest:
                        yield rest
                    return
                await asyncio.sleep(FOLLOW_POLL_S)

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


class SelfTestRequest(BaseModel):
    targets: dict = {}


@app.post("/selftest")
async def selftest(req: SelfTestRequest, x_runner_token: str | None = Header(None)):
    _auth(x_runner_token)
    sys.path.insert(0, str(Path(__file__).parent))
    import selftest as probes  # noqa: PLC0415

    return {"checks": await asyncio.to_thread(probes.run, req.targets)}


# ── the reviewer: one turn of a second, read-only session ───────────────
REVIEWER_HOME = Path(os.environ.get("REVIEWER_HOME", str(STATE_DIR / "reviewer")))
REVIEWER_DENY = "Write Edit MultiEdit NotebookEdit"
CONSULT_TIMEOUT = 540
_consult_lock = asyncio.Lock()


class ConsultRequest(BaseModel):
    run_id: int
    prompt: str
    system_append: str = ""
    model: str = "opus"
    effort: str | None = None
    resume_session: str | None = None


@app.post("/consult")
async def consult(req: ConsultRequest, x_runner_token: str | None = Header(None)):
    """One reviewer turn: a second `claude -p` next to the session, with no hook, no agent
    token, no secrets, and no file-writing tools; /workspace is added for reading. Blocks
    until the turn ends and returns its final text, session id and cost."""
    _auth(x_runner_token)
    if _consult_lock.locked():
        raise HTTPException(409, "a reviewer turn is already in progress")
    async with _consult_lock:
        REVIEWER_HOME.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(prefix="consult-")
        sys_file = Path(tmp.name) / "system.md"
        sys_file.write_text(req.system_append)
        argv = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions", "--model", req.model,
                "--append-system-prompt-file", str(sys_file), "--add-dir", str(WORKSPACE), "--disallowedTools", REVIEWER_DENY]
        if req.effort:
            argv += ["--effort", req.effort]
        if req.resume_session:
            argv += ["--resume", req.resume_session]
        env = {k: v for k, v in os.environ.items() if k not in ("RUNNER_TOKEN", "AGENT_API_TOKEN", "CONTROL_URL")}
        env.update(ASSISTANT_RUN_ID=str(req.run_id), ASSISTANT_RUN_MODE="reviewer")
        proc = await asyncio.create_subprocess_exec(*argv, cwd=REVIEWER_HOME, env=env, start_new_session=True, limit=32 * 1024 * 1024,
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
                    if ev.get("session_id"):
                        out["session_id"] = ev["session_id"]
                    if ev.get("type") == "assistant":
                        msg = ev.get("message") or {}
                        out["model"] = msg.get("model") or out["model"]
                        if msg.get("usage"):
                            out["messages"].append({"id": msg.get("id"), "model": msg.get("model"), "usage": msg["usage"]})
                    elif ev.get("type") == "result":
                        out.update(ok=not ev.get("is_error"), text=ev.get("result") or "", cost_usd=ev.get("total_cost_usd"), usage=ev.get("usage"))
                await proc.wait()

            await asyncio.wait_for(read(), CONSULT_TIMEOUT)
        except TimeoutError:
            out["text"] = f"(the reviewer turn timed out after {CONSULT_TIMEOUT}s)"
        finally:
            with anyio.CancelScope(shield=True):
                await _terminate(proc, grace=2)
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
    return {"ok": True, "busy": _current is not None, "session": _current["id"] if _current else None, "runtime": RUNTIME,
            "runtime_version": version, "logged_in": creds.exists() or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("RUNNER_TOKEN is required")
    recover()
    seed_workspace()
    uvicorn.run(app, host=os.environ.get("RUNNER_HOST", "0.0.0.0"), port=int(os.environ.get("RUNNER_PORT", "8765")), log_level="warning")
