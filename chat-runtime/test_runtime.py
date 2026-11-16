"""Tests for the chat runtime, run against fake_claude.py. Imports nothing from control."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
FAKE = HERE.parent / "agent" / "runner" / "fake_claude.py"
TOKEN, OAUTH, MCP = "rt-secret-token", "oauth-secret-abc", "mcp-secret-xyz"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Mcp(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Mcp.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        if body["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": f"called {body['params']['name']}"}]}
        else:
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
        data = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def mcp():
    srv = HTTPServer(("127.0.0.1", 0), Mcp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rt")
    shim = tmp / "claude"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    shim.chmod(0o755)
    scratch = tmp / "tmpdir"
    scratch.mkdir()
    port = free_port()
    env = {**os.environ, "CHAT_RUNTIME_TOKEN": TOKEN, "CLAUDE_BIN": str(shim), "PORT": str(port), "TMPDIR": str(scratch)}
    proc = subprocess.Popen([sys.executable, str(HERE / "runtime.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(base + "/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base, scratch
    proc.terminate()
    proc.wait(10)


def body(url: str, **kw) -> dict:
    return {"prompt": "hi", "system": "be brief", "model": "sonnet", "effort": None, "oauth_token": OAUTH, "mcp_url": url, "mcp_token": MCP, **kw}


def turn(base: str, payload: dict, token: str | None = TOKEN) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.post(base + "/turn", json=payload, headers=headers, timeout=30)


def test_health_needs_no_auth(runtime):
    r = httpx.get(runtime[0] + "/health")
    assert r.json()["ok"] is True and "fake" in r.json()["claude"]


def test_auth_required(runtime, mcp):
    assert turn(runtime[0], body(mcp), token=None).status_code == 401
    assert turn(runtime[0], body(mcp), token="wrong").status_code == 401


def test_turn_streams_and_ends_with_exit(runtime, mcp):
    r = turn(runtime[0], body(mcp))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(ln) for ln in r.text.splitlines()]
    assert any(ln["type"] == "stream_event" for ln in lines)
    assert any(ln["type"] == "result" and "total_cost_usd" in ln for ln in lines)
    assert lines[-1]["type"] == "runtime" and lines[-1]["event"] == "exit" and lines[-1]["returncode"] == 0
    for secret in (OAUTH, MCP):
        assert secret not in r.text


def test_tool_call_reaches_mcp_with_bearer(runtime, mcp):
    Mcp.calls.clear()
    r = turn(runtime[0], body(mcp, prompt="list please [fake:tool list_projects {}]"))
    assert "called list_projects" in r.text
    assert {c["body"]["method"] for c in Mcp.calls} >= {"initialize", "tools/call"}
    assert all(c["auth"] == f"Bearer {MCP}" for c in Mcp.calls)
    assert MCP not in r.text and OAUTH not in r.text


def test_stderr_is_redacted(runtime, mcp):
    r = turn(runtime[0], body(mcp, mcp_url="http://127.0.0.1:1/mcp", prompt="[fake:tool x {}]"))
    exit_line = json.loads(r.text.splitlines()[-1])
    assert exit_line["event"] == "exit" and MCP not in r.text


@pytest.mark.parametrize("patch", [{"model": "bad model!"}, {"effort": "x;y"}, {"mcp_url": "file:///etc/passwd"}, {"system": "s" * 200_001}])
def test_bad_input_is_400(runtime, mcp, patch):
    assert turn(runtime[0], body(mcp, **patch)).status_code == 400


def test_temp_home_removed(runtime, mcp):
    base, scratch = runtime
    turn(base, body(mcp))
    assert list(scratch.iterdir()) == []
