"""Chat runtime: a stateless sidecar that runs one Claude Code chat turn per request for control.

The Claude subscription's sanctioned client is Claude Code, a CLI with shell tools, so it must
not run inside control (which holds every secret). This is a jail: no volumes, no secrets at
rest, no workspace. It reaches only the egress proxy (for api.anthropic.com) and control's
per-turn MCP endpoint, whose tools are the chat's only tools.

  POST /turn   run one turn; streams the CLI's stream-json lines, then a final
               {"type": "runtime", "event": "exit", "returncode": N, "stderr": "..."} line
  GET  /health

Each turn gets a fresh temporary HOME and cwd, deleted afterwards, and an environment built
from scratch. The tokens in a request (the owner's OAuth token, the MCP token) live only for
the turn and never reach a log or the exit line. Only control can reach this (the chat
network + CHAT_RUNTIME_TOKEN).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

TOKEN = os.environ.get("CHAT_RUNTIME_TOKEN", "")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
MAX_TURNS = 4
TIMEOUT_S = float(os.environ.get("CHAT_TURN_TIMEOUT_S", "600"))
NAME_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,64}$")
PASS_ENV = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"]
LIMIT = 1 << 20

app = FastAPI(title="l0w chat runtime")
active = {"n": 0}


class Turn(BaseModel):
    prompt: str
    system: str
    model: str
    effort: str | None = None
    oauth_token: str
    mcp_url: str
    mcp_token: str


def refusal(status: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def check(turn: Turn) -> str | None:
    if not NAME_RE.match(turn.model):
        return "bad model"
    if turn.effort is not None and not NAME_RE.match(turn.effort):
        return "bad effort"
    if not re.match(r"^https?://[^\s]+$", turn.mcp_url):
        return "mcp_url must be http(s)"
    if len(turn.prompt.encode()) > 2_000_000:
        return "prompt too large"
    if len(turn.system.encode()) > 200_000:
        return "system too large"
    if not turn.oauth_token or not turn.mcp_token:
        return "tokens required"
    return None


def child_env(home: str, turn: Turn) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home, "LANG": os.environ.get("LANG", "C.UTF-8")}
    env.update({k: os.environ[k] for k in PASS_ENV if k in os.environ})
    env.update({
        "NODE_USE_ENV_PROXY": "1", "CLAUDE_CODE_OAUTH_TOKEN": turn.oauth_token, "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    })
    return env


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def exit_line(returncode: int, stderr: bytes, turn: Turn) -> str:
    text = stderr[-2000:].decode("utf-8", "replace")
    for secret in (turn.oauth_token, turn.mcp_token):
        text = text.replace(secret, "[redacted]")
    return json.dumps({"type": "runtime", "event": "exit", "returncode": returncode, "stderr": text}) + "\n"


async def stream_turn(turn: Turn):
    tmp = tempfile.mkdtemp(prefix="chat-")
    proc = None
    try:
        root = Path(tmp)
        home, cwd = root / "home", root / "work"
        home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        write_private(root / "system.md", turn.system)
        write_private(root / "mcp.json", json.dumps({"mcpServers": {"l0w": {"type": "http", "url": turn.mcp_url, "headers": {"Authorization": "Bearer " + turn.mcp_token}}}}))
        argv = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--model", turn.model]
        if turn.effort:
            argv += ["--effort", turn.effort]
        argv += ["--append-system-prompt-file", str(root / "system.md"), "--tools", "", "--strict-mcp-config", "--mcp-config", str(root / "mcp.json"),
                 "--allowedTools", "mcp__l0w", "--no-session-persistence"]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_env(str(home), turn), cwd=str(cwd), start_new_session=True, limit=LIMIT)
        stderr = bytearray()

        async def feed() -> None:
            try:
                proc.stdin.write(turn.prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        async def drain_err() -> None:
            while chunk := await proc.stderr.read(4096):
                stderr.extend(chunk)
                del stderr[:-4000]

        side = [asyncio.create_task(feed()), asyncio.create_task(drain_err())]
        deadline = asyncio.get_event_loop().time() + TIMEOUT_S
        timed_out = False
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except TimeoutError:
                timed_out = True
                break
            except ValueError:
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                json.loads(text)
            except ValueError:
                continue
            yield text + "\n"
        if timed_out:
            kill_group(proc)
        await proc.wait()
        await asyncio.gather(*side, return_exceptions=True)
        yield exit_line(-9 if timed_out else proc.returncode, bytes(stderr), turn)
    finally:
        if proc is not None and proc.returncode is None:
            kill_group(proc)
            try:
                await asyncio.shield(proc.wait())
            except Exception:
                pass
        active["n"] -= 1
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/turn")
async def turn_route(turn: Turn, authorization: str | None = Header(None)):
    if not TOKEN or not secrets.compare_digest((authorization or "").encode(), f"Bearer {TOKEN}".encode()):
        return refusal(401, "unauthorized")
    problem = check(turn)
    if problem:
        return refusal(400, problem)
    if active["n"] >= MAX_TURNS:
        return refusal(503, "busy")
    active["n"] += 1
    return StreamingResponse(stream_turn(turn), media_type="application/x-ndjson")


@app.get("/health")
async def health():
    version = None
    try:
        proc = await asyncio.create_subprocess_exec(CLAUDE, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "")})
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        version = out.decode().strip() or None
    except (TimeoutError, OSError):
        pass
    return {"ok": True, "claude": version}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8770")), log_level="warning")
