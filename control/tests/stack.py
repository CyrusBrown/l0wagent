"""A local stack for integration tests: egress, the real runner driving the fake `claude`,
and control, each a subprocess on 127.0.0.1, so any of them can be killed and restarted.
No Docker, so it tests the protocol and restart handling, not network isolation
(that's `dev/isolation-test.sh` against the compose stack)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "agent" / "runner"
TOKENS = {"agent": "agent-token-test", "runner": "runner-token-test", "egress": "egress-token-test"}
PASSWORD = "correct horse battery"
GW_CONTROL, GW_TUNNEL = "gw-control-token-test", "gw-tunnel-token-test"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, headers: dict | None = None, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, headers=headers or {}, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise TimeoutError(url)


class Stack:
    def __init__(self, tmp: Path, steps: int = 8, delay: float = 0.4, usage_policy: str = "allow", own_groups: bool = True, hold_minutes: float = 0.1,
                 runner_env: dict | None = None, control_env: dict | None = None):
        self.tmp = tmp
        self.runner_env, self.control_env = runner_env or {}, control_env or {}
        self.hold_minutes = hold_minutes
        (tmp / "work" / "_inbox").mkdir(parents=True, exist_ok=True)
        # Tests put each process in its own group (to kill it with its children). The dev
        # stack keeps them in its group, so whoever stops it (Playwright) stops them too.
        self.own_groups = own_groups
        self.ports = {k: free_port() for k in ("control", "runner", "proxy", "admin", "files")}
        self.procs: dict[str, subprocess.Popen] = {}
        self.steps, self.delay, self.usage_policy = steps, delay, usage_policy
        bindir = tmp / "bin"
        bindir.mkdir(exist_ok=True)
        claude = bindir / "claude"
        claude.write_text(f"#!/bin/sh\nexec {sys.executable} {RUNNER / 'fake_claude.py'} \"$@\"\n")
        claude.chmod(0o755)
        opencode = bindir / "opencode"
        opencode.write_text(f"#!/bin/sh\nexec {sys.executable} {RUNNER / 'fake_opencode.py'} \"$@\"\n")
        opencode.chmod(0o755)
        assistant = bindir / "assistant"
        assistant.write_text(f"#!/bin/sh\nexec {sys.executable} {REPO / 'agent' / 'bin' / 'assistant'} \"$@\"\n")
        assistant.chmod(0o755)
        hook = bindir / "assistant-hook"
        hook.write_text(f"#!/bin/sh\nexec {sys.executable} {REPO / 'agent' / 'bin' / 'assistant-hook'} \"$@\"\n")
        hook.chmod(0o755)
        self.bindir = bindir
        self.url = f"http://127.0.0.1:{self.ports['control']}"
        self.client = httpx.Client(base_url=self.url, timeout=30, headers={"x-l0w-client": "test"})

    def _spawn(self, name: str, argv: list[str], env: dict) -> None:
        log = open(self.tmp / f"{name}.log", "ab")
        self.procs[name] = subprocess.Popen(argv, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT, start_new_session=self.own_groups)

    def start_egress(self) -> None:
        self._spawn("egress", [sys.executable, str(REPO / "egress" / "egress.py")], {
            "EGRESS_TOKEN": TOKENS["egress"], "DATA_DIR": str(self.tmp / "egress"), "BIND": "127.0.0.1",
            "PROXY_PORT": str(self.ports["proxy"]), "ADMIN_PORT": str(self.ports["admin"]), "SANDBOX_SUBNET": "10.255.255.0/24",
        })
        wait_http(f"http://127.0.0.1:{self.ports['admin']}/status")

    def start_runner(self) -> None:
        self._spawn("runner", [sys.executable, str(RUNNER / "runner.py")], {
            "RUNNER_TOKEN": TOKENS["runner"], "WORKSPACE": str(self.tmp / "workspace"), "RUNNER_STATE_DIR": str(self.tmp / "runner"),
            "RUNNER_SEED": str(self.tmp / "no-seed"), "CLAUDE_BIN": str(self.bindir / "claude"), "CLAUDE_SETTINGS": "/nonexistent",
            "OPENCODE_BIN": str(self.bindir / "opencode"),
            "RUNNER_HOST": "127.0.0.1", "RUNNER_PORT": str(self.ports["runner"]), "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "FAKE_STEPS": str(self.steps),
            "WORK_DIR": str(self.tmp / "work"), "FAKE_RERUN_DELAY": "3", "FAKE_STEP_DELAY": str(self.delay), "FAKE_CALL_ASSISTANT": "1", "FAKE_CLAUDE_STATE": str(self.tmp / "fake-claude"),
            "CONTROL_URL": self.url, "AGENT_API_TOKEN": TOKENS["agent"],
            # The agent's home: never the developer's own (the fake `claude auth login` writes credentials there).
            "HOME": str(self.tmp / "agent-home"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            **self.runner_env,
        })
        wait_http(f"http://127.0.0.1:{self.ports['runner']}/health", {"X-Runner-Token": TOKENS["runner"]})

    def start_control(self) -> None:
        self._spawn("control", [sys.executable, "-m", "uvicorn", "l0w_control.app:app", "--host", "127.0.0.1", "--port", str(self.ports["control"])], {
            "L0W_BACKEND": "sqlite", "L0W_DATA_DIR": str(self.tmp / "control"), "L0W_AGENT_TOKEN": TOKENS["agent"],
            "L0W_RUNNER_URL": f"http://127.0.0.1:{self.ports['runner']}", "L0W_RUNNER_TOKEN": TOKENS["runner"],
            "L0W_EGRESS_URL": f"http://127.0.0.1:{self.ports['admin']}", "L0W_EGRESS_TOKEN": TOKENS["egress"],
            "L0W_AGENT_HOME": str(self.tmp / "agent-home"), "L0W_USAGE_URL": "http://127.0.0.1:9/none",
            "L0W_SANDBOX_SUBNET": "10.255.255.0/24", "USAGE_UNKNOWN_POLICY": self.usage_policy, "L0W_INBOX_DIR": str(self.tmp / "work" / "_inbox"),
            "L0W_WORK_DIR": str(self.tmp / "work"), "L0W_WORKSPACE_DIR": str(self.tmp / "workspace"), "L0W_FILES_PORT": str(self.ports["files"]),
            "L0W_FILES_HOST": "127.0.0.1",
            "HOLD_MINUTES": str(self.hold_minutes), "L0W_SCHEDULER": "0", "L0W_AGENT_API_ANY_ADDRESS": "1",  # the runner is on localhost here
            **self.control_env,
        })
        wait_http(f"{self.url}/api/meta")

    def kill(self, name: str, sig: int = signal.SIGKILL) -> None:
        p = self.procs.pop(name)
        if self.own_groups:
            os.killpg(p.pid, sig)
        else:
            p.send_signal(sig)
        p.wait(10)

    def up(self) -> Stack:
        self.start_egress()
        self.start_control()
        self.start_runner()
        return self

    def down(self) -> None:
        for name in list(self.procs):
            try:
                self.kill(name)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        self.client.close()

    # ── helpers ────────────────────────────────────────────────────────────
    def login(self) -> None:
        if not self.client.get("/api/meta").json()["setup_complete"]:
            r = self.client.post("/api/setup/owner", json={"username": "owner", "password": PASSWORD, "timezone": "UTC"})
            assert r.status_code == 200, r.text
        r = self.client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
        assert r.status_code == 200, r.text

    def lease_active(self) -> bool:
        return httpx.get(f"http://127.0.0.1:{self.ports['admin']}/status", headers={"X-Egress-Token": TOKENS["egress"]}).json()["lease_active"]

    def run(self, run_id: int) -> dict:
        r = self.client.get(f"/api/runs/{run_id}")
        r.raise_for_status()
        return r.json()["run"]

    def timeline(self, run_id: int, view: str = "detailed") -> list[dict]:
        return self.client.get(f"/api/runs/{run_id}/timeline", params={"view": view}).json()

    def wait_for(self, pred, timeout: float = 60, what: str = "condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                value = pred()
                if value:
                    return value
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        logs = "\n".join(f"── {p.name}\n{p.read_text()[-3000:]}" for p in self.tmp.glob("*.log"))
        raise AssertionError(f"timed out waiting for {what}\n{logs}")


class HubStack(Stack):
    """A Stack plus the fake hub, the gateway and the tunnel service (dev_stack.py --hub uses it too)."""

    def __init__(self, tmp: Path, hub_port: int | None = None, control_env: dict | None = None, **kw):
        self.hub_port, self.gw_port = hub_port or free_port(), free_port()
        self.hub_base = f"http://hub.localhost:{self.hub_port}"
        super().__init__(tmp, **kw, control_env={
            "L0W_GATEWAY_URL": f"http://127.0.0.1:{self.gw_port}", "L0W_GATEWAY_TOKEN": GW_CONTROL,
            "L0W_VERSION": "0.9.0", "L0W_HUB_ALLOW_HTTP": "1", "L0W_HUB_CONNECT_TO": f"127.0.0.1:{self.hub_port}",
            **(control_env or {}),
        })

    def _uv(self, project: str, *args: str) -> list[str]:
        return ["uv", "run", "--quiet", "--project", str(REPO / project), "python", *args]

    def start_hub(self) -> None:
        self._spawn("hub", self._uv("dev/hub", "-m", "fakehub", "--base-url", self.hub_base, "--state", str(self.tmp / "hub" / "state.json"),
                                    "serve", "--port", str(self.hub_port)), {})
        wait_http(f"http://127.0.0.1:{self.hub_port}/.well-known/l0wagent/hub", {"Host": f"hub.localhost:{self.hub_port}"}, timeout=60)

    def invite(self) -> str:
        out = subprocess.run(self._uv("dev/hub", "-m", "fakehub", "--base-url", self.hub_base, "--state", str(self.tmp / "hub" / "state.json"), "invite"),
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def start_gateway(self) -> None:
        self._spawn("gateway", self._uv("gateway", "-m", "l0w_gateway"), {
            "GATEWAY_DATA_DIR": str(self.tmp / "gateway"), "GATEWAY_CONTROL_TOKEN": GW_CONTROL, "GATEWAY_TUNNEL_TOKEN": GW_TUNNEL,
            "GATEWAY_PORT": str(self.gw_port)})
        wait_http(f"http://127.0.0.1:{self.gw_port}/healthz", timeout=60)

    def start_tunnel(self) -> None:
        self._spawn("tunnel", self._uv("tunnel", "-m", "l0w_tunnel"), {
            "TUNNEL_GATEWAY_URL": f"http://127.0.0.1:{self.gw_port}", "TUNNEL_GATEWAY_TOKEN": GW_TUNNEL,
            "TUNNEL_CONTROL_URL": self.url, "TUNNEL_FILES_URL": f"http://127.0.0.1:{self.ports['files']}", "TUNNEL_POLL_SECONDS": "0.5"})

    def up(self, runner: bool = False) -> HubStack:
        (self.tmp / "hub").mkdir(exist_ok=True)
        self.start_hub()
        self.start_gateway()
        self.start_tunnel()
        self.start_egress()
        self.start_control()
        if runner:
            self.start_runner()
        return self

    def visit(self, host: str, path: str, **kw) -> httpx.Response:
        """A visitor on the internet: through the hub's front door, by Host."""
        return httpx.get(f"http://127.0.0.1:{self.hub_port}{path}", headers={"Host": host, **kw.pop("headers", {})}, timeout=15, **kw)
