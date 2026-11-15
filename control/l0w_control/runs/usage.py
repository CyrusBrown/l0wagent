"""Claude subscription usage (5-hour and weekly %) and the usage gate.

Sources, best first:
1. "oauth": GET /api/oauth/usage with the access token from the agent's `claude auth login`
   credentials (the agent's home, mounted read-only into control). Tokens from
   `claude setup-token` lack the scope for it, so a full login is needed.
2. "stream": the latest rate_limit_events the runtime reported during runs (last 6 hours).
3. Nothing: usage is unknown, and the gate refuses runs unless USAGE_UNKNOWN_POLICY=allow.

The gate fails closed: an unknown reading, or one at or above the owner's reserve, keeps
runs from starting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from ..api import schemas as s
from ..auth import iso, parse, utcnow
from ..config import Env, Settings
from ..db import Database

log = logging.getLogger("l0w.usage")
PROVIDER = "claude-subscription"


@dataclass
class Gate:
    open: bool
    reason: str | None = None


def _pct(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v


def _norm_util(v) -> float | None:
    v = _pct(v)
    if v is None:
        return None
    return v * 100.0 if v <= 1.0 else v


def _dt(v) -> datetime | None:
    """Unix seconds or ISO text → an aware datetime."""
    if v is None:
        return None
    if isinstance(v, int | float):
        return datetime.fromtimestamp(v, UTC)
    try:
        return parse(str(v))
    except ValueError:
        return None


class Usage:
    def __init__(self, env: Env, db: Database, settings: Settings):
        self.env = env
        self.db = db
        self.settings = settings
        self._cache: s.UsageReading | None = None

    # ── readings ───────────────────────────────────────────────────────────
    def _credentials(self) -> tuple[str | None, str | None, bool]:
        """(access token, error, expired)."""
        path = self.env.agent_home / ".claude" / ".credentials.json"
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None, "the agent isn't logged in to Claude", False
        except (OSError, json.JSONDecodeError) as e:
            return None, f"can't read the agent's Claude login: {e}", False
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            return None, "the agent's Claude login has no access token", False
        expires = oauth.get("expiresAt")
        if isinstance(expires, int | float) and expires / 1000 <= utcnow().timestamp() + 60:
            return token, "the agent's Claude login token expired", True
        return token, None, False

    async def _oauth(self) -> tuple[s.UsageReading, bool]:
        now = utcnow()
        token, err, expired = self._credentials()
        reading = s.UsageReading(provider=PROVIDER, source="oauth", fetched_at=now, five_hour_pct=None, five_hour_resets_at=None,
                                 seven_day_pct=None, seven_day_resets_at=None, error=err)
        if err:
            return reading, expired
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(self.env.usage_url, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
        except httpx.HTTPError as e:
            reading.error = f"usage request failed: {e}"
            return reading, False
        if r.status_code == 401:
            reading.error = "usage endpoint: 401 (login expired or revoked)"
            return reading, True
        if r.status_code != 200:
            reading.error = f"usage endpoint: HTTP {r.status_code}"
            return reading, False
        data = r.json()
        five, week = data.get("five_hour") or {}, data.get("seven_day") or {}
        reading.five_hour_pct = _pct(five.get("utilization"))
        reading.five_hour_resets_at = _dt(five.get("resets_at"))
        reading.seven_day_pct = _pct(week.get("utilization"))
        reading.seven_day_resets_at = _dt(week.get("resets_at"))
        if reading.seven_day_pct is None:
            reading.error = "the usage response had no weekly figure"
        return reading, False

    def _stream(self) -> s.UsageReading:
        reading = s.UsageReading(provider=PROVIDER, source="stream", fetched_at=utcnow(), five_hour_pct=None, five_hour_resets_at=None,
                                 seven_day_pct=None, seven_day_resets_at=None, error=None)
        since = iso(utcnow() - timedelta(hours=6))
        for window, attr in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
            row = self.db.q1("SELECT * FROM rate_limit_events WHERE limit_type=? AND at>=? ORDER BY id DESC LIMIT 1", (window, since))
            if not row:
                continue
            util = row["utilization"]
            if util is None and row["status"] == "rejected":
                util = 100.0
            setattr(reading, f"{attr}_pct", util)
            setattr(reading, f"{attr}_resets_at", _dt(row["resets_at"]))
        if reading.seven_day_pct is None:
            reading.error = "no recent usage reports from runs"
        return reading

    async def read(self, max_age: float = 120, refresh=None) -> s.UsageReading:
        """A reading no older than max_age seconds. `refresh` (async callable) is tried
        once when the agent's login token has expired."""
        if self._cache and (utcnow() - self._cache.fetched_at).total_seconds() < max_age:
            return self._cache
        reading, expired = await self._oauth()
        if expired and refresh is not None:
            try:
                await refresh()
                reading, _ = await self._oauth()
            except Exception as e:  # noqa: BLE001
                reading.error = f"{reading.error}; refreshing the login failed: {e}"
        if reading.seven_day_pct is None:
            fallback = self._stream()
            if fallback.seven_day_pct is not None:
                fallback.error = f"oauth unavailable: {reading.error}"
                reading = fallback
        self._store(reading)
        self._cache = reading
        return reading

    def cached(self) -> s.UsageReading | None:
        if self._cache:
            return self._cache
        row = self.db.q1("SELECT * FROM usage_snapshots ORDER BY id DESC LIMIT 1")
        if not row:
            return None
        return s.UsageReading(provider=row["provider"], source=row["source"], fetched_at=_dt(row["fetched_at"]), five_hour_pct=row["five_hour_pct"],  # type: ignore[arg-type]
                              five_hour_resets_at=_dt(row["five_hour_resets_at"]), seven_day_pct=row["seven_day_pct"],
                              seven_day_resets_at=_dt(row["seven_day_resets_at"]), error=row["error"])

    def _store(self, u: s.UsageReading) -> None:
        self.db.ex(
            "INSERT INTO usage_snapshots(provider, fetched_at, source, five_hour_pct, five_hour_resets_at, seven_day_pct, seven_day_resets_at, error) VALUES(?,?,?,?,?,?,?,?)",
            (u.provider, iso(u.fetched_at), u.source, u.five_hour_pct, u.five_hour_resets_at and iso(u.five_hour_resets_at), u.seven_day_pct,
             u.seven_day_resets_at and iso(u.seven_day_resets_at), u.error),
        )
        self.db.ex("DELETE FROM usage_snapshots WHERE fetched_at < ?", (iso(utcnow() - timedelta(days=30)),))

    def record_rate_limit(self, run_id: int, info: dict) -> None:
        """Store a runtime's rate-limit report (each window it includes)."""
        primary = info.get("rateLimitType") or info.get("rate_limit_type")
        windows = dict(info.get("unifiedWindows") or {})
        if primary and primary not in windows:
            windows[primary] = {"utilization": info.get("utilization"), "resetsAt": info.get("resetsAt") or info.get("resets_at")}
        for window, w in windows.items():
            w = w or {}
            self.db.ex(
                "INSERT INTO rate_limit_events(at, run_id, limit_type, status, resets_at, utilization) VALUES(?,?,?,?,?,?)",
                (self.db.now(), run_id, window, info.get("status") if window == primary else "allowed", (lambda d: d and iso(d))(_dt(w.get("resetsAt"))), _norm_util(w.get("utilization"))),
            )
        self._cache = None  # the next read may now fall back to these

    # ── the gate ───────────────────────────────────────────────────────────
    def gate(self, u: s.UsageReading | None, starting: bool = True) -> Gate:
        if u is None or u.seven_day_pct is None:
            if str(self.settings.get("USAGE_UNKNOWN_POLICY")).lower() == "allow":
                return Gate(True, None)
            return Gate(False, f"Usage can't be read ({(u and u.error) or 'no reading'}), so runs don't start. Log the agent in to Claude, or set USAGE_UNKNOWN_POLICY=allow.")
        weekly, five = self.settings.get("RESERVE_WEEKLY_PCT"), self.settings.get("RESERVE_FIVE_HOUR_PCT")
        if starting and u.seven_day_pct >= weekly:
            return Gate(False, f"Weekly usage is at {u.seven_day_pct:.0f}%, at or above your reserve ({weekly:.0f}%).")
        if u.five_hour_pct is not None and u.five_hour_pct >= five:
            return Gate(False, f"5-hour usage is at {u.five_hour_pct:.0f}%, at or above your reserve ({five:.0f}%).")
        return Gate(True, None)
