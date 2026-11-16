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
from typing import Any


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
    instance_name: str = field(default_factory=lambda: _env("L0W_INSTANCE_NAME", "l0wagent"))


# key: (type, default, description)
SPECS: dict[str, tuple[str, Any, str]] = {
    "USAGE_UNKNOWN_POLICY": ("str", "refuse", "'refuse' (fail closed) or 'allow': whether runs may start when usage can't be read."),
    "RESERVE_WEEKLY_PCT": ("float", 80.0, "Don't start a run at or above this weekly usage (%), so the owner keeps room."),
    "RESERVE_FIVE_HOUR_PCT": ("float", 70.0, "Don't start a run, and stop a running one, at or above this 5-hour usage (%)."),
    "MANUAL_RUN_MAX_MINUTES": ("int", 60, "Wall-clock cap for a run started by the owner."),
    "WAKEUP_MAX_MINUTES": ("int", 180, "Wall-clock cap for a planned wakeup or routine run."),
    "MANUAL_RUN_PTS": ("float", 20.0, "Budget (pts) for a run you start with Wake now, unless you give one."),
    "REPLY_RUN_MAX_PTS": ("float", 6.0, "Most a single Reply-now run may spend from the reply budget (pts)."),
    "SWEEP_WINDOW_HOURS": ("float", 5.0, "Sweep: how many hours before the weekly reset sweep wakeups may run."),
    "SWEEP_CEILING_PCT": ("float", 95.0, "Sweep: the weekly usage (%) sweep wakeups won't go past."),
    "WAKEUP_LATE_MINUTES": ("int", 30, "A planned wakeup that can't start within this many minutes of its time (another run is busy) is marked missed."),
    "PROMOTE_WAIT_MINUTES": ("float", 10.0, "How long a reply run's promote request stays open before it expires."),
    "DEFAULT_TIER": ("str", "sonnet", "Model (optionally model/effort) for runs that don't name one."),
    "REPLY_TIER": ("str", "sonnet", "Model (optionally model/effort) for Reply-now runs."),
    "REPLY_MAX_MINUTES": ("int", 15, "Wall-clock cap for a Reply-now run, including time held open for your reply."),
    "HOLD_MINUTES": ("float", 5.0, "If the assistant messaged you or asked something, hold the run open this long for your reply (0 = off)."),
    "HOLD_MAX_ROUNDS": ("int", 3, "How many times one run may wait for and resume on your replies."),
    "RUN_MAX_RESUMES": ("int", 3, "How many times a run is resumed after the agent computer restarts mid-run."),
    "STOP_GRACE_SECONDS": ("int", 90, "After asking a session to stop, force it this many seconds later."),
    "LOGIN_MAX_FAILURES_PER_IP": ("int", 10, "Failed logins from one address within 15 minutes before it's rate-limited."),
    "LOGIN_LOCKOUT_FAILURES": ("int", 5, "Consecutive failed logins for the account before a temporary lockout."),
    "LOGIN_LOCKOUT_MINUTES": ("int", 15, "Length of the temporary lockout."),
    "SESSION_DAYS": ("int", 30, "Browser sessions expire after this many days without use."),
    "SELFTEST_LAN_TARGETS": ("str", "host.docker.internal:22,192.168.1.1:80,10.0.0.1:80", "LAN/host addresses the self-test checks the agent can't reach (host:port, comma-separated)."),
}


def cast(kind: str, raw: Any) -> Any:
    if kind == "int":
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    return str(raw)


class Settings:
    """Reads and writes SPECS through the `settings` table."""

    def __init__(self, db):
        self.db = db

    def get(self, key: str) -> Any:
        kind, default, _ = SPECS[key]
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
        kind, _, _ = SPECS[key]
        value = cast(kind, value)
        self.db.ex(
            "INSERT INTO settings(key, value_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, json.dumps(value), self.db.now()),
        )
