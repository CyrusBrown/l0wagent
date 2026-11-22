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
    # The hub tunnel service's network: callers from it are the "tunnel" class (guard.py, netaddr.py).
    tunnel_subnet: str = field(default_factory=lambda: _env("L0W_TUNNEL_SUBNET"))
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
    "HOLD_MAX_ROUNDS": Spec("int", 10, "How many times one run may wait for and resume on your replies.", "agent", 0, 50),
    "USE_WHOLE_BUDGET": Spec("bool", True, "Use it or lose it: a wakeup keeps working (backlog, free-time queue, new tasks it writes) until its budget is spent, "
                             "instead of stopping when the planned work is done. Busywork still doesn't count. Off: a wakeup's budget is planned and it may stop early."),
    "NUDGE_MAX": Spec("int", 2, "How many times a wakeup that stops on its own with budget left is told to carry on.", lo=0, hi=10),
    "NUDGE_MIN_LEFT_PCT": Spec("float", 15.0, "Only nudge a wakeup that stopped with more than this share of its budget left (%).", lo=0, hi=100),
    "RUN_MAX_RESUMES": Spec("int", 3, "How many times a run is resumed after the agent computer restarts mid-run.", lo=0, hi=20),
    "STOP_GRACE_SECONDS": Spec("int", 90, "After asking a session to stop, force it this many seconds later.", lo=1, hi=3600),
    "LOGIN_MAX_FAILURES_PER_IP": Spec("int", 10, "Failed logins from one address within 15 minutes before it's rate-limited.", lo=1, hi=1000),
    "RUN_EVENTS_KEEP_DAYS": Spec("int", 30, "Delete the raw event log of finished runs after this many days (the run's timeline stays).", lo=1, hi=3650),
    "SESSION_DAYS": Spec("int", 30, "Browser sessions expire after this many days without use.", lo=1, hi=365),
    "DISK_WARN_FREE_GB": Spec("float", 5.0, "Warn (Settings → System, and one notification a day) when the server's free disk space drops below this many GB.", lo=0.5, hi=1000),
    "SELFTEST_LAN_TARGETS": Spec("str", "host.docker.internal:22,192.168.1.1:80,10.0.0.1:80", "LAN/host addresses the self-test checks the agent can't reach (host:port, comma-separated)."),
    "REVIEWER_GATE_MAX_ROUNDS": Spec("int", 3, "How many times the reviewer's Gate may send a run back to the assistant before letting it finish.", lo=0, hi=10),
    "FALLBACK_NIGHTS": Spec("int", 2, "Control plans a fallback wakeup when the assistant has nothing planned for this many nights ahead.", lo=1, hi=7),
    "BRIEF_LIST_CAP": Spec("int", 15, "Items per list in a wakeup's brief (open claims, asks waiting on you); the rest are a command away.", lo=3, hi=100),
    "NEWS_CAP": Spec("int", 6, "Harness news entries shown in one brief (the rest via `assistant news`).", lo=1, hi=30),
    "CHAT_MAX_TOKENS": Spec("int", 32000, "The longest reply (in tokens) a 1:1 chat on an Anthropic key may write in one step.", lo=1000, hi=128000),
    "INTEGRATION_TIMEOUT_S": Spec("int", 60, "How long control waits for an integration's (MCP server's) answer.", lo=5, hi=600),
    "ZIP_MAX_MB": Spec("int", 1024, "The largest folder download (zip), before compression.", lo=10, hi=20480),
    "ZIP_MAX_FILES": Spec("int", 50000, "The most files in one folder download.", lo=100, hi=1000000),
    "FALLBACK_HOUR": Spec("int", 3, "The hour (your local time) control plans a fallback wakeup at when the assistant has planned nothing.", lo=0, hi=23),
    "PLAN_HORIZON_DAYS": Spec("int", 8, "How many days ahead a wakeup may be planned.", lo=1, hi=31),
    "MAX_OPEN_ASKS_PER_RUN": Spec("int", 20, "How many asks (questions, permissions, tasks) one run may have open at once.", lo=1, hi=200),
    "MAX_ASK_NOTIFICATIONS_PER_HOUR": Spec("int", 10, "Ask notifications sent per hour before the rest are folded into one.", lo=1, hi=200),
    "UPLOAD_MAX_MB": Spec("int", 200, "The largest file you can upload to the assistant (MB).", lo=1, hi=4096),
    "SHELF_SNAPSHOT_MAX_MB": Spec("int", 500, "The largest file a shelf item will snapshot or serve (MB).", lo=1, hi=4096),
    "CHAT_MAX_STEPS": Spec("int", 12, "How many tool steps the chat may take before it answers.", lo=1, hi=50),
    "LOST_CONTACT_GIVE_UP_MINUTES": Spec("int", 10, "How long the agent computer may stay silent before a run is given up on.", lo=2, hi=120),
    "LOGIN_HISTORY_DAYS": Spec("int", 30, "Delete sign-in history older than this many days.", lo=1, hi=3650),
    "FRIEND_REQUESTS_FROM": Spec("str", "hub", "Who may send you friend requests: 'anyone', 'hub' (members of your hub) or 'codes' (only people with a friend code you made). "
                                 "A request with a valid code always gets through. Others are dropped silently."),
    "PEER_PAUSED": Spec("bool", False, "Pause all peer activity (friends' messages, requests and presence): incoming envelopes are refused so senders retry later, and nothing goes out."),
    "PRESENCE_INTERVAL_MINUTES": Spec("int", 10, "Friends hear at once when your assistant's state changes (asleep, working, idle); what it is doing now, at most this often.",
                                      lo=1, hi=1440),
    "WAKE_ASK_LIMIT_PER_DAY": Spec("int", 3, "How many times a day one friend's request to wake your assistant may ask you ('ask me' friends); more are answered 'later'.",
                                   lo=0, hi=50),
    "WAKE_ASK_EXPIRE_HOURS": Spec("int", 24, "A wake request you haven't answered after this many hours is withdrawn and answered 'later'.", lo=1, hi=720),
    "WAKE_FRIEND_PTS": Spec("float", 5.0, "The budget (pts) of a wakeup a friend's allowed wake request schedules; it counts against your allowance.", lo=0.5, hi=1000),
    "WAKE_SEND_LIMIT_PER_DAY": Spec("int", 3, "How many times a day your assistant may ask one friend's assistant to wake.", lo=0, hi=50),
    "FRIEND_CODE_EXPIRE_DAYS": Spec("int", 14, "How long a new friend code stays valid, unless you pick another time when you make it.", lo=1, hi=90),
    "PEER_PENDING_GIVE_UP_HOURS": Spec("int", 24, "A message to a friend the gateway never took (it was down, or peer activity paused) is given up on after this many hours.",
                                       lo=1, hi=168),
    "PEER_RUN_PCT": Spec("float", 20.0, "The spend breaker: peer spending (a run a friend woke, and the turns spent on friends' messages in other runs) may use this share (%) of "
                         "one run's budget before replies to friends pause and you're asked.", lo=1, hi=100),
    "PEER_DAY_PCT": Spec("float", 5.0, "The spend breaker: peer spending may use this share (%) of the period's allowance per local day before replies to friends pause and you're asked.",
                         lo=0.5, hi=100),
    "PEER_DAY_PCT_STEP": Spec("float", 5.0, "How many points of percent 'Allow more today' adds to today's limit on peer spending.", lo=0.5, hi=100),
    "PEER_SPAN_MINUTES": Spec("int", 10, "Turns spent on a friend's message count as peer spending until the assistant posts elsewhere, the run ends, "
                              "or this many minutes pass after the last friend message.",
                              lo=1, hi=240),
}


# Settings → Advanced (and the chat's `advanced.<KEY>` settings): the tunables above that have no screen of
# their own, grouped and labeled. Left out on purpose: USAGE_UNKNOWN_POLICY (the usage gate fails closed; only
# the host's environment can loosen it), DEFAULT_TIER and REPLY_TIER (roles set these now), and the reserve and
# sweep numbers (Settings → Budget). key → (group, label, unit)
ADVANCED: dict[str, tuple[str, str, str | None]] = {
    "HOLD_MINUTES": ("Conversation", "Wait for your reply", "min"),
    "HOLD_MAX_ROUNDS": ("Conversation", "Replies one run may wait for", None),
    "PROMOTE_WAIT_MINUTES": ("Conversation", "Promote request stays open", "min"),
    "MAX_OPEN_ASKS_PER_RUN": ("Conversation", "Open asks one run may file", None),
    "USE_WHOLE_BUDGET": ("Runs", "Use the whole budget (use it or lose it)", None),
    "NUDGE_MAX": ("Runs", "Times a wakeup that stops early is told to carry on", None),
    "NUDGE_MIN_LEFT_PCT": ("Runs", "Carry on only if more than this is left", "%"),
    "REPLY_RUN_MAX_PTS": ("Runs", "Most one Reply now may spend", "pts"),
    "REPLY_MAX_MINUTES": ("Runs", "Reply now time limit", "min"),
    "MANUAL_RUN_PTS": ("Runs", "Wake now budget", "pts"),
    "MANUAL_RUN_MAX_MINUTES": ("Runs", "Wake now time limit", "min"),
    "WAKEUP_MAX_MINUTES": ("Runs", "Wakeup and routine time limit", "min"),
    "WAKEUP_LATE_MINUTES": ("Runs", "A wakeup counts as missed after", "min"),
    "RUN_MAX_RESUMES": ("Runs", "Resumes after the agent computer restarts", None),
    "STOP_GRACE_SECONDS": ("Runs", "Force a stop after", "s"),
    "LOST_CONTACT_GIVE_UP_MINUTES": ("Runs", "Give up on a silent agent computer after", "min"),
    "BRIEF_LIST_CAP": ("Wakeups", "Items per list in the brief", None),
    "NEWS_CAP": ("Wakeups", "News entries per brief", None),
    "FALLBACK_NIGHTS": ("Wakeups", "Fallback when nothing is planned for", "nights"),
    "FALLBACK_HOUR": ("Wakeups", "Fallback wakeup hour", "h"),
    "PLAN_HORIZON_DAYS": ("Wakeups", "How far ahead it may plan", "days"),
    "REVIEWER_GATE_MAX_ROUNDS": ("Reviewer", "Times the reviewer can send it back (Gate)", None),
    "CHAT_MAX_STEPS": ("Chat", "Tool steps per chat reply", None),
    "CHAT_MAX_TOKENS": ("Chat", "Longest chat reply step (Anthropic key)", "tokens"),
    "UPLOAD_MAX_MB": ("Files", "Largest upload", "MB"),
    "SHELF_SNAPSHOT_MAX_MB": ("Files", "Largest shelf snapshot", "MB"),
    "ZIP_MAX_MB": ("Files", "Largest folder download", "MB"),
    "ZIP_MAX_FILES": ("Files", "Most files in a folder download", None),
    "INTEGRATION_TIMEOUT_S": ("Files", "Integration call timeout", "s"),
    "MAX_ASK_NOTIFICATIONS_PER_HOUR": ("Notifications", "Ask notifications per hour", None),
    "PRESENCE_INTERVAL_MINUTES": ("Friends", "Friends hear what it is doing at most every", "min"),
    "WAKE_ASK_LIMIT_PER_DAY": ("Friends", "Wake requests that may ask you, per friend a day", None),
    "WAKE_ASK_EXPIRE_HOURS": ("Friends", "Unanswered wake request is withdrawn after", "h"),
    "WAKE_FRIEND_PTS": ("Friends", "Budget of a wakeup a friend asked for", "pts"),
    "WAKE_SEND_LIMIT_PER_DAY": ("Friends", "Times your assistant may ask one friend to wake", None),
    "FRIEND_CODE_EXPIRE_DAYS": ("Friends", "New friend code lasts", "days"),
    "PEER_PENDING_GIVE_UP_HOURS": ("Friends", "Give up on a message the gateway never took after", "h"),
    "PEER_RUN_PCT": ("Friends", "Spend breaker: most of one run's budget for friends", "%"),
    "PEER_DAY_PCT": ("Friends", "Spend breaker: most of the allowance for friends per day", "%"),
    "PEER_DAY_PCT_STEP": ("Friends", "Spend breaker: 'Allow more today' adds", "%"),
    "PEER_SPAN_MINUTES": ("Friends", "Friend conversation counts as peer spending for", "min"),
    "SESSION_DAYS": ("System", "Sign out idle browsers after", "days"),
    "LOGIN_MAX_FAILURES_PER_IP": ("System", "Failed logins per address (15 min)", None),
    "LOGIN_HISTORY_DAYS": ("System", "Keep sign-in history for", "days"),
    "RUN_EVENTS_KEEP_DAYS": ("System", "Keep raw run logs for", "days"),
    "DISK_WARN_FREE_GB": ("System", "Warn when free disk is below", "GB"),
    "UPDATE_CHECK": ("System", "Check for updates", None),
    "SELFTEST_LAN_TARGETS": ("System", "Self-test LAN addresses", None),
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


def advanced_view(get) -> list:
    """Settings → Advanced, with current values from `get(key)`."""
    from .api import schemas as s

    out = []
    for key, (group, label, unit) in ADVANCED.items():
        sp = SPECS[key]
        out.append(s.AdvancedSetting(key=key, group=group, label=label, help=sp.desc, kind=sp.kind, value=get(key), default=sp.default,  # type: ignore[arg-type]
                                     min=sp.lo, max=sp.hi, unit=unit))
    return out
