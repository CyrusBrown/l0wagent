"""Configuration.

Two kinds:
- **Deployment constants** (tokens, URLs, paths, the sandbox subnet) come only from the
  environment, read once into `Env`.
- **Settings** (`SPECS`) are tunables. Precedence: a value saved by the owner (the
  `settings` table), then the environment variable of the same name, then the default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Env:
    data_dir: Path = field(default_factory=lambda: Path(_env("L0W_DATA_DIR", "./data")))
    # The agent's token for /api/agent/*. Required for the agent API to work at all.
    agent_token: str = field(default_factory=lambda: _env("L0W_AGENT_TOKEN"))
    # Clients from this subnet (the agent's network) may only call /api/agent/*.
    sandbox_subnet: str = field(default_factory=lambda: _env("L0W_SANDBOX_SUBNET", "172.31.240.0/24"))
    runner_url: str = field(default_factory=lambda: _env("L0W_RUNNER_URL", "http://agent:8765").rstrip("/"))
    runner_token: str = field(default_factory=lambda: _env("L0W_RUNNER_TOKEN"))
    egress_url: str = field(default_factory=lambda: _env("L0W_EGRESS_URL", "http://egress-admin:8090").rstrip("/"))
    egress_token: str = field(default_factory=lambda: _env("L0W_EGRESS_TOKEN"))
    # The agent's home, mounted read-only: control reads the Claude login there to read usage.
    agent_home: Path = field(default_factory=lambda: Path(_env("L0W_AGENT_HOME", "/agent-home")))
    usage_url: str = field(default_factory=lambda: _env("L0W_USAGE_URL", "https://api.anthropic.com/api/oauth/usage"))
    # Set the session cookie's Secure flag. "auto" = only when the request came over HTTPS.
    cookie_secure: str = field(default_factory=lambda: _env("L0W_COOKIE_SECURE", "auto"))
    # Reverse proxies / tunnels whose forwarding headers are believed (addresses or subnets, comma-separated).
    trusted_proxies: str = field(default_factory=lambda: _env("L0W_TRUSTED_PROXIES"))
    # One-time code the first owner setup must present (./l0w install writes it). Unset = loopback/private callers only.
    setup_code: str = field(default_factory=lambda: _env("L0W_SETUP_CODE"))
    instance_name: str = field(default_factory=lambda: _env("L0W_INSTANCE_NAME", "l0wagent"))
    # The chat-runtime sidecar (Claude-subscription chats). Its subnet may only call /api/chat-runtime/*.
    chat_runtime_url: str = field(default_factory=lambda: _env("L0W_CHAT_RUNTIME_URL").rstrip("/"))
    chat_runtime_token: str = field(default_factory=lambda: _env("L0W_CHAT_RUNTIME_TOKEN"))
    chat_subnet: str = field(default_factory=lambda: _env("L0W_CHAT_SUBNET"))
    # How the sidecar reaches control (for its per-turn MCP endpoint).
    chat_control_url: str = field(default_factory=lambda: _env("L0W_CHAT_CONTROL_URL", "http://control:8000").rstrip("/"))
    # Dev only (dev_stack.py, tests): let /api/agent/* be called from any address, not just the sandbox subnet.
    agent_api_any_address: bool = field(default_factory=lambda: _env("L0W_AGENT_API_ANY_ADDRESS") == "1")


class Spec(NamedTuple):
    kind: str
    default: Any
    desc: str
    who: str = "owner"  # who may propose a change: "agent" (run-shape settings only) or "owner"
    lo: float | None = None
    hi: float | None = None


SPECS: dict[str, Spec] = {
    "USAGE_UNKNOWN_POLICY": Spec("str", "refuse", "'refuse' (fail closed) or 'allow': whether runs may start when usage can't be read."),
    "RESERVE_WEEKLY_PCT": Spec("float", 80.0, "Don't start a run at or above this weekly usage (%), so the owner keeps room.", lo=0, hi=100),
    "RESERVE_FIVE_HOUR_PCT": Spec("float", 70.0, "Don't start a run, and stop a running one, at or above this 5-hour usage (%).", lo=0, hi=100),
    "MANUAL_RUN_MAX_MINUTES": Spec("int", 60, "Wall-clock cap for a run started by the owner.", lo=1, hi=1440),
    "WAKEUP_MAX_MINUTES": Spec("int", 180, "Wall-clock cap for a planned wakeup or routine run.", "agent", 1, 1440),
    "MANUAL_RUN_PTS": Spec("float", 20.0, "Budget (pts) for a run you start with Wake now, unless you give one.", lo=0, hi=1000),
    "REPLY_RUN_MAX_PTS": Spec("float", 6.0, "Most a single Reply-now run may spend from the reply budget (pts).", lo=0, hi=1000),
    "SWEEP_WINDOW_HOURS": Spec("float", 5.0, "Sweep: how many hours before the weekly reset sweep wakeups may run.", lo=0, hi=48),
    "SWEEP_CEILING_PCT": Spec("float", 95.0, "Sweep: the weekly usage (%) sweep wakeups won't go past.", lo=0, hi=100),
    "WAKEUP_LATE_MINUTES": Spec("int", 30, "A planned wakeup that can't start within this many minutes of its time (another run is busy) is marked missed.", lo=1, hi=1440),
    "UPDATE_CHECK": Spec("bool", True, "Check GitHub for new stable releases every few hours (shows the Update available banner)."),
    "PROMOTE_WAIT_MINUTES": Spec("float", 10.0, "How long a reply run's promote request stays open before it expires.", "agent", 1, 120),
    "DEFAULT_TIER": Spec("str", "sonnet", "Model (optionally model/effort) for runs that don't name one."),
    "REPLY_TIER": Spec("str", "sonnet", "Model (optionally model/effort) for Reply-now runs."),
    "REPLY_MAX_MINUTES": Spec("int", 15, "Wall-clock cap for a Reply-now run, including time held open for your reply.", "agent", 1, 240),
    "HOLD_MINUTES": Spec("float", 5.0, "If the assistant messaged you or asked something, hold the run open this long for your reply (0 = off).", "agent", 0, 60),
    "HOLD_MAX_ROUNDS": Spec("int", 3, "How many times one run may wait for and resume on your replies.", "agent", 0, 20),
    "RUN_MAX_RESUMES": Spec("int", 3, "How many times a run is resumed after the agent computer restarts mid-run.", lo=0, hi=20),
    "STOP_GRACE_SECONDS": Spec("int", 90, "After asking a session to stop, force it this many seconds later.", lo=1, hi=3600),
    "LOGIN_MAX_FAILURES_PER_IP": Spec("int", 10, "Failed logins from one address within 15 minutes before it's rate-limited.", lo=1, hi=1000),
    "RUN_EVENTS_KEEP_DAYS": Spec("int", 30, "Delete the raw event log of finished runs after this many days (the run's timeline stays).", lo=1, hi=3650),
    "SESSION_DAYS": Spec("int", 30, "Browser sessions expire after this many days without use.", lo=1, hi=365),
    "DISK_WARN_FREE_GB": Spec("float", 5.0, "Warn (Settings → System, and one notification a day) when the server's free disk space drops below this many GB.", lo=0.5, hi=1000),
    "SELFTEST_LAN_TARGETS": Spec("str", "host.docker.internal:22,192.168.1.1:80,10.0.0.1:80", "LAN/host addresses the self-test checks the agent can't reach (host:port, comma-separated)."),
}


def cast(kind: str, raw: Any) -> Any:
    if kind == "int":
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    return str(raw)


def check_range(key: str, value: Any) -> None:
    """Raises ValueError when a number is outside the setting's range."""
    spec = SPECS[key]
    if spec.kind in ("int", "float") and ((spec.lo is not None and value < spec.lo) or (spec.hi is not None and value > spec.hi)):
        raise ValueError(f"{key} must be between {spec.lo:g} and {spec.hi:g}.")


class Settings:
    """Reads and writes SPECS through the `settings` table."""

    def __init__(self, db):
        self.db = db

    def get(self, key: str) -> Any:
        kind, default = SPECS[key][:2]
        row = self.db.q1("SELECT value_json FROM settings WHERE key=?", (key,))
        candidates = [json.loads(row["value_json"])] if row else []
        if key in os.environ:
            candidates.append(os.environ[key])
        for raw in candidates:
            try:
                return cast(kind, raw)
            except (TypeError, ValueError):
                continue
        return default

    def set(self, key: str, value: Any) -> None:
        spec = SPECS[key]
        value = cast(spec.kind, value)
        check_range(key, value)
        self.db.ex(
            "INSERT INTO settings(key, value_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value), self.db.now()),
        )
