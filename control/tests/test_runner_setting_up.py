"""While the agent container sets itself up, a stand-in answers on the runner's port (agent/runner/setup_status.py);
control reports "setting up" instead of "unreachable" and runs wait for it."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from l0w_control.runs import manager as mgr
from l0w_control.runs.clients import RunnerClient, RunnerError, RunnerSettingUp

STAND_IN = Path(__file__).resolve().parents[2] / "agent" / "runner" / "setup_status.py"


@pytest.fixture
def stand_in(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    phase = tmp_path / "phase"
    phase.write_text("provisioning\n")
    p = subprocess.Popen([sys.executable, str(STAND_IN)], env={**os.environ, "RUNNER_HOST": "127.0.0.1", "RUNNER_PORT": str(port),
                                                              "SETUP_PHASE_FILE": str(phase)})
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", phase
    p.terminate()
    p.wait(5)


def client(url):
    return RunnerClient(SimpleNamespace(runner_url=url, runner_token="t"))


def test_the_stand_in_reads_as_setting_up_with_its_phase(stand_in):
    async def go():
        url, phase = stand_in
        with pytest.raises(RunnerSettingUp) as e:
            await client(url).health()
        assert e.value.phase == "provisioning" and "reinstalling its tools" in str(e.value) and e.value.since
        phase.write_text("boot")
        with pytest.raises(RunnerSettingUp) as e:
            await client(url).start({})
        assert e.value.phase == "boot"
    asyncio.run(go())


def test_other_503s_stay_plain_runner_errors():
    async def go():
        with pytest.raises(RunnerError) as e:
            await client("http://127.0.0.1:9").health()
        assert not isinstance(e.value, RunnerSettingUp)
    asyncio.run(go())


class FakeRunner:
    def __init__(self, setting_up_for: int):
        self.left = setting_up_for

    async def health(self):
        if self.left > 0:
            self.left -= 1
            raise RunnerSettingUp("provisioning", "2026-11-19T02:24:19Z")
        return {"busy": False}


def fake_manager(runner, stop=False):
    items = []
    m = SimpleNamespace(runner=runner, timeline=lambda run_id, item: items.append(item), _stop_requested=lambda run_id: stop)
    return m, items


def test_a_run_waits_with_one_note_then_starts(monkeypatch):
    async def go():
        monkeypatch.setattr(mgr, "SETUP_POLL_S", 0)
        m, items = fake_manager(FakeRunner(3))
        await mgr.RunManager._wait_until_set_up(m, 1)
        assert m.runner.left == 0
        assert [(i.kind, i.simple) for i in items] == [("control", True)] and "starts when it's ready" in items[0].text
    asyncio.run(go())


def test_a_stopped_run_stops_waiting(monkeypatch):
    async def go():
        monkeypatch.setattr(mgr, "SETUP_POLL_S", 0)
        m, _ = fake_manager(FakeRunner(100), stop=True)
        with pytest.raises(RunnerSettingUp):
            await mgr.RunManager._wait_until_set_up(m, 1)
    asyncio.run(go())


def test_waiting_gives_up_after_the_limit(monkeypatch):
    async def go():
        monkeypatch.setattr(mgr, "SETUP_POLL_S", 0)
        monkeypatch.setattr(mgr, "SETUP_WAIT_S", 0)
        m, _ = fake_manager(FakeRunner(100))
        with pytest.raises(RunnerSettingUp):
            await asyncio.wait_for(mgr.RunManager._wait_until_set_up(m, 1), 5)
    asyncio.run(go())


class HandoverRunner:
    """Setting up, then refused for a moment (the stand-in stopped, the runner not yet listening), then up."""

    def __init__(self):
        self.calls = 0

    async def health(self):
        self.calls += 1
        if self.calls == 1:
            raise RunnerSettingUp("boot")
        if self.calls == 2:
            raise RunnerError("runner unreachable: ConnectError")
        return {"busy": False}


def test_the_handover_from_the_stand_in_to_the_runner_is_waited_out(monkeypatch):
    monkeypatch.setattr(mgr, "SETUP_POLL_S", 0)
    monkeypatch.setattr(mgr.asyncio, "sleep", _no_sleep)
    m, _ = fake_manager(HandoverRunner())
    asyncio.run(mgr.RunManager._wait_until_set_up(m, 1))
    assert m.runner.calls == 3


def test_an_unreachable_runner_without_setup_is_left_to_the_start(monkeypatch):
    class Down:
        calls = 0

        async def health(self):
            self.calls += 1
            raise RunnerError("runner unreachable")
    m, _ = fake_manager(Down())
    asyncio.run(mgr.RunManager._wait_until_set_up(m, 1))
    assert m.runner.calls == 1


async def _no_sleep(_s):
    return None
