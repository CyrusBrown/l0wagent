"""Issue #1: a quiet session isn't a lost one. The runner sends a heartbeat line on a quiet log
stream, which control counts as contact but not as log bytes; an open stream that goes quiet
past the idle timeout reconnects without "Lost touch" on the timeline."""

import asyncio
import json
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from l0w_control.config import Env
from l0w_control.runs import clients
from l0w_control.runs.clients import RunnerClient, RunnerIdle

from .stack import REPO

sys.path.insert(0, str(REPO / "agent" / "runner"))
import runner  # noqa: E402


def test_the_runner_sends_heartbeats_on_a_quiet_log(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TOKEN", "t")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "HEARTBEAT_S", 0.3)
    (tmp_path / "sessions").mkdir()
    runner._log("s1").write_bytes(json.dumps({"type": "x"}).encode() + b"\n")
    runner._save_meta("s1", id="s1", state="running")

    def end_soon():
        time.sleep(1.2)
        runner._save_meta("s1", state="exited")

    import threading
    threading.Thread(target=end_soon, daemon=True).start()
    with TestClient(runner.app) as c:
        body = c.get("/sessions/s1/log", params={"offset": 0}, headers={"X-Runner-Token": "t"}).content
    assert body.startswith(b'{"type": "x"}\n') and body.count(runner.HEARTBEAT) >= 2


def test_the_client_counts_heartbeats_as_contact_not_bytes(monkeypatch):
    line = json.dumps({"type": "x"}).encode() + b"\n"

    def handler(request):
        return httpx.Response(200, content=b"\x1e\n" + line + b"\x1e\n")

    real = httpx.AsyncClient
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    rc = RunnerClient(Env(runner_url="http://runner", runner_token="t"))

    async def collect():
        return [x async for x in rc.log("s1", 0)]

    got = asyncio.run(collect())
    assert got == [(None, 0), (None, 0), ({"type": "x"}, len(line)), (None, 0)]


def test_an_idle_open_stream_is_not_lost_contact(monkeypatch):
    class Quiet(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout("quiet")
            yield b""  # pragma: no cover

    real = httpx.AsyncClient
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Quiet())), **kw))
    rc = RunnerClient(Env(runner_url="http://runner", runner_token="t"))

    async def drain():
        return [x async for x in rc.log("s1", 0)]

    with pytest.raises(RunnerIdle):
        asyncio.run(drain())
