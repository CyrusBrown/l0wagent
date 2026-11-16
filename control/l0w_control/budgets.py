"""Budgets: providers, points, levels, the usage ledger and wakeup validation.

**Points** (owner, 2026-11-15): one budget unit. On a Claude subscription 1 pt = 0.1% of the
week (1,000 pts a week); on an API key 1 pt = $0.10.

**Where the numbers come from**
- A Claude subscription's meter gives the week's total (weekly % × 10). What the assistant
  used comes from the ledger: each session's tokens priced at API rates (the runtime's own
  `total_cost_usd` once it reports it), converted to points with a calibration learned from
  how far the weekly meter moved during runs (`pts_per_usd`). The owner's own use is the
  rest: total − assistant − chat.
- An API key: cost × 10.

**The allowance** is what the assistant may plan with this week: the level's (or the owner's
override, or a Boost's), minus what it already used, and never more than the room left below
the owner's weekly reserve, so the owner's own use shrinks it automatically. Reply-now runs
spend the separate **reply budget**; sweep wakeups spend only what the owner left unused
near the reset, on top of the allowance.

Times are evaluated in the owner's time zone for allowed hours and the API week.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, NotFound
from .config import Settings
from .db import Database

SUBSCRIPTION = "claude-subscription"
API = "anthropic-api"
UNIT = "pts"
DEFAULT_PTS_PER_USD = 2.0       # Claude subscription, until calibrated
PLAN_HORIZON_DAYS = 8
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
WEEKDAYS = [0, 1, 2, 3, 4]

# API prices per million tokens (input, output), by model family: for the live estimate only.
# The runtime's own total replaces it when the session ends.
PRICES = {"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
CACHE_READ, CACHE_WRITE = 0.1, 1.25

TIERS = ["haiku/low", "sonnet/low", "sonnet/medium", "sonnet/high", "opus/low", "opus/medium", "opus/high"]


@dataclass
class Limits:
    allowance: float
    allowed_hours: list[dict]      # [{"days": [0..6] (Mon=0), "start": "HH:MM", "end": "HH:MM"}]; end <= start crosses midnight
    max_per_day: int
    min_gap_minutes: int
    max_wakeup: float
    tiers: list[str]
    sweep: bool
    fallback: bool


LEVELS: dict[str, tuple[str, Limits]] = {
    "dormant": ("No scheduled wakeups. Reply now still works, on its own budget.",
                Limits(0, [], 0, 0, 0, [], False, False)),
    "frugal": ("Wakes only for planned work and replies, a few short night wakeups on a small model. Long tasks get split over several nights.",
               Limits(50, [{"days": ALL_DAYS, "start": "00:00", "end": "07:00"}], 1, 240, 15, ["haiku/low", "sonnet/low", "sonnet/medium"], False, True)),
    "steady": ("About one wakeup a night on a mid-size model, and a reserve for your replies.",
               Limits(150, [{"days": ALL_DAYS, "start": "22:00", "end": "08:00"}], 2, 120, 40, ["sonnet/low", "sonnet/medium", "sonnet/high", "opus/low"], False, True)),
    "busy": ("Nightly plus off-hours wakeups, longer reviews, and the sweep before the weekly reset. Good for a week with a deadline.",
             Limits(400, [{"days": WEEKDAYS, "start": "18:00", "end": "09:00"}, {"days": [5, 6], "start": "00:00", "end": "00:00"}], 6, 60, 80,
                    ["sonnet/low", "sonnet/medium", "sonnet/high", "opus/low", "opus/medium"], True, True)),
    "max": ("Around the clock, paced per 5-hour window, with the sweep. Reserves still protect your own use.",
            Limits(850, [{"days": ALL_DAYS, "start": "00:00", "end": "00:00"}], 24, 20, 150, list(TIERS), True, True)),
}
LEVEL_ORDER = list(LEVELS)
LIMIT_KEYS = [f for f in Limits.__dataclass_fields__]


@dataclass
class Effective:
    provider: str
    level: str                     # the level in force (the Boost's while one is active)
    base_level: str
    boost: dict | None
    limits: Limits
    reply_pts: float
    custom: list[str] = field(default_factory=list)   # overridden fields


def default_config(kind: str) -> dict:
    return {"level": "steady", "overrides": {}, "boost": None, "reply_pts": 30.0 if kind == SUBSCRIPTION else 10.0,
            "fallback_pts": None, "chat_cap": None}


def _hm(v: str) -> time:
    h, m = (int(x) for x in v.split(":"))
    return time(h % 24, m)


def in_hours(windows: list[dict], local: datetime) -> bool:
    """Is a local time inside the allowed windows? A window belongs to its start day; one
    whose end is at or before its start runs past midnight (00:00–00:00 is the whole day)."""
    for w in windows:
        start, end = _hm(w["start"]), _hm(w["end"])
        days = w.get("days", ALL_DAYS)
        t = local.time()
        if end <= start:
            if local.weekday() in days and t >= start:
                return True
            if (local.weekday() - 1) % 7 in days and t < end:
                return True
        elif local.weekday() in days and start <= t < end:
            return True
    return False


def next_in_hours(windows: list[dict], after_local: datetime, step_minutes: int = 15) -> datetime | None:
    """The first time at or after `after_local` (rounded up to the step) inside the windows."""
    if not windows:
        return None
    t = after_local.replace(second=0, microsecond=0)
    if t.minute % step_minutes:
        t += timedelta(minutes=step_minutes - t.minute % step_minutes)
    for _ in range(PLAN_HORIZON_DAYS * 24 * 60 // step_minutes):
        if in_hours(windows, t):
            return t
        t += timedelta(minutes=step_minutes)
    return None


def hours_text(windows: list[dict]) -> str:
    if not windows:
        return "none"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = []
    for w in windows:
        days = sorted(w.get("days", ALL_DAYS))
        span = "all day" if w["start"] == w["end"] else f"{w['start']}–{w['end']}"
        d = "" if days == ALL_DAYS else (" weekdays" if days == WEEKDAYS else " weekends" if days == [5, 6] else " " + ",".join(names[i] for i in days))
        out.append(f"{span}{d}")
    return ", ".join(out)


def price(model: str | None, usage: dict) -> float:
    fam = next((f for f in PRICES if f in (model or "").lower()), "sonnet")
    pin, pout = PRICES[fam]
    return (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout
            + usage.get("cache_read_input_tokens", 0) * pin * CACHE_READ + usage.get("cache_creation_input_tokens", 0) * pin * CACHE_WRITE) / 1e6


class Budgets:
    def __init__(self, db: Database, settings: Settings, usage, fernet):
        self.db, self.settings, self.usage, self.fernet = db, settings, usage, fernet
        self.now = utcnow   # replaced in tests to simulate time
        self._seen: dict[int, set[str]] = {}   # session row → message ids already counted

    # ── providers ──────────────────────────────────────────────────────────
    def ensure_defaults(self) -> None:
        if not self.db.q1("SELECT 1 FROM providers WHERE id=?", (SUBSCRIPTION,)):
            now = self.db.now()
            self.db.ex("INSERT INTO providers(id, kind, label, enabled, config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                       (SUBSCRIPTION, SUBSCRIPTION, "Claude subscription", 1, json.dumps(default_config(SUBSCRIPTION)), now, now))

    def _row(self, pid: str) -> dict:
        row = self.db.q1("SELECT * FROM providers WHERE id=?", (pid,))
        if not row:
            raise NotFound(f"provider {pid}")
        return row

    def provider_ids(self, enabled_only: bool = True) -> list[str]:
        return [r["id"] for r in self.db.q("SELECT id FROM providers WHERE (? = 0 OR enabled=1) ORDER BY created_at, id", (int(enabled_only),))]

    def config(self, pid: str) -> dict:
        row = self._row(pid)
        return {**default_config(row["kind"]), **json.loads(row["config_json"] or "{}")}

    def save_config(self, pid: str, cfg: dict) -> None:
        self.db.ex("UPDATE providers SET config_json=?, updated_at=? WHERE id=?", (json.dumps(cfg), self.db.now(), pid))

    def credential(self, pid: str) -> str | None:
        row = self._row(pid)
        return self.fernet.decrypt(row["credential_enc"]).decode() if row["credential_enc"] else None

    def add_api_provider(self, key: str, label: str | None) -> None:
        key = key.strip()
        if not key.startswith("sk-ant-") or len(key) < 20:
            raise BadRequest("bad_key", "That doesn't look like an Anthropic API key (they start with sk-ant-).")
        now = self.db.now()
        enc = self.fernet.encrypt(key.encode())
        if self.db.q1("SELECT 1 FROM providers WHERE id=?", (API,)):
            self.db.ex("UPDATE providers SET credential_enc=?, label=COALESCE(?, label), enabled=1, updated_at=? WHERE id=?", (enc, label, now, API))
        else:
            self.db.ex("INSERT INTO providers(id, kind, label, enabled, credential_enc, config_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (API, API, label or "Anthropic API", 1, enc, json.dumps(default_config(API)), now, now))

    def provider_env(self, pid: str) -> dict[str, str]:
        """Extra environment for a session on this provider (an API key goes only to sessions that use it)."""
        if pid == API:
            key = self.credential(pid)
            return {"ANTHROPIC_API_KEY": key} if key else {}
        return {}

    # ── time ───────────────────────────────────────────────────────────────
    def tz(self) -> ZoneInfo:
        row = self.db.q1("SELECT timezone FROM users WHERE id=1")
        try:
            return ZoneInfo(row["timezone"]) if row and row["timezone"] else ZoneInfo("UTC")
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz())

    def week(self, pid: str, now: datetime | None = None) -> tuple[datetime, datetime]:
        """The provider's budget week: a subscription's weekly window, else Monday 00:00 local."""
        now = now or self.now()
        if pid == SUBSCRIPTION:
            u = self.usage.cached()
            if u and u.seven_day_resets_at:
                end = u.seven_day_resets_at
                while end <= now:
                    end += timedelta(days=7)
                return end - timedelta(days=7), end
        loc = self.local(now)
        start = (loc - timedelta(days=loc.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)

    # ── levels ─────────────────────────────────────────────────────────────
    def effective(self, pid: str, now: datetime | None = None) -> Effective:
        now = now or self.now()
        cfg = self.config(pid)
        boost = cfg.get("boost")
        if boost and parse(boost["until"]) <= now:  # type: ignore[operator]
            boost = None
        base = cfg.get("level") if cfg.get("level") in LEVELS else "steady"
        level = boost["level"] if boost else base
        limits = Limits(**asdict(LEVELS[level][1]))
        custom = []
        if not boost:  # a Boost uses its level's limits as they are
            for k, v in (cfg.get("overrides") or {}).items():
                if k in LIMIT_KEYS and v is not None:
                    setattr(limits, k, v)
                    custom.append(k)
        return Effective(pid, level, base, boost, limits, float(cfg.get("reply_pts") or 0), custom)

    def set_budget(self, pid: str, patch: s.BudgetPatch) -> None:
        cfg = self.config(pid)
        if patch.level is not None:
            cfg["level"] = patch.level
            cfg["overrides"] = {}
        if patch.overrides is not None:
            ov = dict(cfg.get("overrides") or {})
            for k, v in patch.overrides.model_dump(exclude_unset=True).items():
                if v is None:
                    ov.pop(k, None)
                else:
                    ov[k] = v
            for w in ov.get("allowed_hours") or []:
                for key in ("start", "end"):
                    try:
                        _hm(w[key])
                    except (KeyError, ValueError) as e:
                        raise BadRequest("bad_hours", "Allowed hours look like {\"days\": [0..6], \"start\": \"HH:MM\", \"end\": \"HH:MM\"}.") from e
            bad = [t for t in ov.get("tiers") or [] if t not in TIERS]
            if bad:
                raise BadRequest("bad_tier", f"Unknown tier(s): {', '.join(bad)}. Known: {', '.join(TIERS)}.")
            cfg["overrides"] = ov
        if patch.reply_pts is not None:
            cfg["reply_pts"] = max(0.0, patch.reply_pts)
        if patch.fallback_pts is not None:
            cfg["fallback_pts"] = max(0.0, patch.fallback_pts) or None
        self.save_config(pid, cfg)

    def set_boost(self, pid: str, req: s.BoostRequest) -> None:
        now = self.now()
        cfg = self.config(pid)
        if req.level not in LEVELS or LEVEL_ORDER.index(req.level) <= LEVEL_ORDER.index(cfg.get("level", "steady")):
            raise BadRequest("not_a_boost", "A Boost raises the level: pick one above your current level.")
        if req.until_reset:
            until = self.week(pid, now)[1]
        elif req.until:
            until = req.until
        else:
            until = now + timedelta(hours=req.hours or 24)
        if until <= now:
            raise BadRequest("past", "The Boost would already be over.")
        cfg["boost"] = {"level": req.level, "until": iso(until)}
        self.save_config(pid, cfg)

    def clear_boost(self, pid: str) -> None:
        cfg = self.config(pid)
        cfg["boost"] = None
        self.save_config(pid, cfg)

    # ── points ─────────────────────────────────────────────────────────────
    def pts_per_usd(self, pid: str) -> float:
        if pid != SUBSCRIPTION:
            return 10.0
        row = self.db.q1("SELECT value FROM kv WHERE key='pts_per_usd'")
        try:
            return float(row["value"]) if row else DEFAULT_PTS_PER_USD
        except ValueError:
            return DEFAULT_PTS_PER_USD

    def calibrate(self, delta_pts: float, cost_usd: float) -> None:
        """Learn the subscription's points per API-dollar from a run's weekly-meter delta.
        Only runs that moved the meter at least 1% count (the meter's resolution)."""
        if delta_pts < 10 or cost_usd <= 0.05:
            return
        sample = min(50.0, max(0.05, delta_pts / cost_usd))
        k = 0.7 * self.pts_per_usd(SUBSCRIPTION) + 0.3 * sample
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('pts_per_usd', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (f"{k:.4f}", self.db.now()))

    def _ledger_row(self, run_id: int | None, session_id: int | None, pid: str, who: str, bucket: str) -> dict:
        row = self.db.q1("SELECT * FROM usage_ledger WHERE run_id IS ? AND session_id IS ? AND who=? AND bucket=?", (run_id, session_id, who, bucket))
        if row:
            return row
        now = self.db.now()
        lid = self.db.ex("INSERT INTO usage_ledger(provider, run_id, session_id, who, bucket, at, updated_at) VALUES(?,?,?,?,?,?,?)",
                         (pid, run_id, session_id, who, bucket, now, now))
        return self.db.q1("SELECT * FROM usage_ledger WHERE id=?", (lid,))  # type: ignore[return-value]

    def record_message(self, run_id: int | None, session_id: int, pid: str, who: str, bucket: str, message_id: str | None, model: str | None, usage: dict) -> float:
        """Count one model message's tokens (once per message id). Returns the points added."""
        seen = self._seen.setdefault(session_id, set())
        if message_id and message_id in seen:
            return 0.0
        if message_id:
            seen.add(message_id)
        cost = price(model, usage)
        pts = cost * self.pts_per_usd(pid)
        row = self._ledger_row(run_id, session_id, pid, who, bucket)
        if row["cost_final"]:
            return 0.0
        self.db.ex("UPDATE usage_ledger SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, cache_read_tokens=cache_read_tokens+?, cache_write_tokens=cache_write_tokens+?, "
                   "cost_usd=cost_usd+?, pts=pts+?, updated_at=? WHERE id=?",
                   (usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0),
                    cost, pts, self.db.now(), row["id"]))
        return pts

    def record_total(self, run_id: int | None, session_id: int, pid: str, who: str, bucket: str, cost_usd: float | None) -> None:
        """The runtime's own total for a session replaces the estimate. A session that spans
        buckets (a reply promoted mid-session) keeps the estimate's split and scales it."""
        if cost_usd is None:
            return
        rows = self.db.q("SELECT * FROM usage_ledger WHERE session_id=? AND who=?", (session_id, who))
        if not rows:
            rows = [self._ledger_row(run_id, session_id, pid, who, bucket)]
        est = sum(r["cost_usd"] for r in rows)
        k = self.pts_per_usd(pid)
        for r in rows:
            share = (r["cost_usd"] / est) if est > 0 else (1.0 / len(rows))
            c = cost_usd * share
            self.db.ex("UPDATE usage_ledger SET cost_usd=?, pts=?, cost_final=1, updated_at=? WHERE id=?", (c, c * k, self.db.now(), r["id"]))
        self._seen.pop(session_id, None)

    def run_pts(self, run_id: int) -> float:
        return float(self.db.q1("SELECT COALESCE(SUM(pts), 0) AS p FROM usage_ledger WHERE run_id=?", (run_id,))["p"])

    def run_cost(self, run_id: int) -> float:
        return float(self.db.q1("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage_ledger WHERE run_id=?", (run_id,))["c"])

    def spent(self, pid: str, start: datetime, end: datetime, who: tuple[str, ...] = ("assistant", "reviewer"), bucket: str | None = None) -> float:
        sql = f"SELECT COALESCE(SUM(pts), 0) AS p FROM usage_ledger WHERE provider=? AND at>=? AND at<? AND who IN ({','.join('?' * len(who))})"
        params: list[Any] = [pid, iso(start), iso(end), *who]
        if bucket:
            sql += " AND bucket=?"
            params.append(bucket)
        return float(self.db.q1(sql, params)["p"])

    # ── the summary ────────────────────────────────────────────────────────
    def scheduled(self, pid: str, start: datetime, end: datetime, exclude_id: int | None = None) -> float:
        row = self.db.q1("SELECT COALESCE(SUM(budget), 0) AS b FROM wakeups WHERE provider=? AND status='planned' AND kind != 'sweep' AND at>=? AND at<? AND id IS NOT ?",
                         (pid, iso(start), iso(end), exclude_id))
        return float(row["b"])

    def left(self, pid: str, now: datetime | None = None) -> float:
        """Allowance left this week, after the owner's reserve."""
        return self.summary(pid, now).left

    def reply_left(self, pid: str, now: datetime | None = None) -> float:
        now = now or self.now()
        start, end = self.week(pid, now)
        return max(0.0, self.effective(pid, now).reply_pts - self.spent(pid, start, end, bucket="reply"))

    def summary(self, pid: str, now: datetime | None = None) -> s.ProviderBudget:
        now = now or self.now()
        row = self._row(pid)
        eff = self.effective(pid, now)
        start, end = self.week(pid, now)
        assistant = self.spent(pid, start, end)
        allowance_used = self.spent(pid, start, end, bucket="allowance")
        chat = self.spent(pid, start, end, who=("chat",))
        reading = self.usage.cached() if pid == SUBSCRIPTION else None
        total = reading.seven_day_pct * 10 if reading and reading.seven_day_pct is not None else None
        owner = max(0.0, total - assistant - chat) if total is not None else 0.0
        left = eff.limits.allowance - allowance_used
        room = None
        if total is not None:
            room = max(0.0, self.settings.get("RESERVE_WEEKLY_PCT") * 10 - total)
            left = min(left, room)
        left = max(0.0, left)
        scheduled = self.scheduled(pid, max(now, start), end)
        days_left = max((end - now).total_seconds() / 86400, 0.01)
        unscheduled = left - scheduled
        pace = (f"{unscheduled:.0f} {UNIT} unscheduled, {days_left:.1f} days left: ~{max(unscheduled, 0) / days_left:.0f}/day"
                if eff.limits.allowance > 0 else "Dormant: no scheduled wakeups")
        native = (f"{reading.seven_day_pct:.0f}% of the week used" if reading and reading.seven_day_pct is not None else
                  ("weekly usage unknown" if pid == SUBSCRIPTION else f"${self.spent(pid, start, end, who=('assistant', 'reviewer', 'chat')) / 10:.2f} this week"))
        return s.ProviderBudget(
            provider=pid, kind=row["kind"], label=row["label"], enabled=bool(row["enabled"]), unit=UNIT, level=eff.base_level, effective_level=eff.level,  # type: ignore[arg-type]
            boost=s.Boost(level=eff.boost["level"], until=parse(eff.boost["until"])) if eff.boost else None,  # type: ignore[arg-type]
            limits=s.BudgetLimits(**asdict(eff.limits)), custom=eff.custom, allowance=eff.limits.allowance, left=round(left, 1),
            used_by_assistant=round(assistant, 1), used_by_owner=round(owner, 1), used_by_chat=round(chat, 1), room_below_reserve=room and round(room, 1),
            reply_allowance=eff.reply_pts, reply_left=round(max(0.0, eff.reply_pts - self.spent(pid, start, end, bucket="reply")), 1),
            scheduled=round(scheduled, 1), week_start=start, resets_at=end, pace=pace, native=native, pts_per_usd=round(self.pts_per_usd(pid), 3),
            fallback_pts=self.fallback_pts(pid, now), has_credential=bool(row["credential_enc"]) or pid == SUBSCRIPTION,
        )

    def budget_summary(self, pid: str) -> s.BudgetSummary:
        p = self.summary(pid)
        return s.BudgetSummary(unit=UNIT, left=p.left, allowance=p.allowance, used_by_assistant=p.used_by_assistant, used_by_owner=p.used_by_owner,
                               used_by_chat=p.used_by_chat, resets_at=p.resets_at)

    def fallback_pts(self, pid: str, now: datetime | None = None) -> float:
        eff = self.effective(pid, now)
        cfg = self.config(pid)
        if cfg.get("fallback_pts"):
            return float(min(cfg["fallback_pts"], eff.limits.max_wakeup or cfg["fallback_pts"]))
        return float(round(min(eff.limits.max_wakeup, max(5.0, eff.limits.allowance * 0.1))))

    # ── validating a planned wakeup ────────────────────────────────────────
    def validate(self, pid: str, at: datetime, budget: float, tier: str, *, exclude_id: int | None = None, by: str = "assistant") -> str | None:
        """Why a wakeup can't be planned, or None. The owner's own additions skip the
        assistant's limits (hours, count, gap, tiers) but not the arithmetic."""
        now = self.now()
        eff = self.effective(pid, now)
        lim = eff.limits
        if at < now + timedelta(minutes=1):
            return "That time has passed: plan wakeups at least a minute ahead."
        if at > now + timedelta(days=PLAN_HORIZON_DAYS):
            return f"Plan at most {PLAN_HORIZON_DAYS} days ahead."
        if budget <= 0:
            return "The budget must be more than 0 pts."
        if by == "assistant":
            if lim.allowance <= 0:
                return f"The level is {eff.level.capitalize()}: no scheduled wakeups."
            if budget > lim.max_wakeup:
                return f"At most {lim.max_wakeup:g} {UNIT} per wakeup at the {eff.level.capitalize()} level."
            if tier not in lim.tiers:
                return f"Tier {tier} isn't allowed. Allowed: {', '.join(lim.tiers)}."
            if not in_hours(lim.allowed_hours, self.local(at)):
                return f"{self.local(at):%a %H:%M} is outside the allowed hours ({hours_text(lim.allowed_hours)})."
            day = self.local(at).date()
            same_day = [r for r in self.db.q("SELECT at FROM wakeups WHERE provider=? AND status IN ('planned','running','done') AND kind IN ('planned','fallback','owner') AND id IS NOT ?",
                                             (pid, exclude_id)) if self.local(parse(r["at"])).date() == day]  # type: ignore[arg-type]
            if len(same_day) >= lim.max_per_day:
                return f"Already {len(same_day)} wakeup(s) on {day:%a %b %d}; the limit is {lim.max_per_day} a day."
            gap = timedelta(minutes=lim.min_gap_minutes)
            near = self.db.q1("SELECT at FROM wakeups WHERE provider=? AND status='planned' AND kind != 'sweep' AND at>? AND at<? AND id IS NOT ? LIMIT 1",
                              (pid, iso(at - gap), iso(at + gap), exclude_id))
            if near:
                return f"Too close to the wakeup at {self.local(parse(near['at'])):%a %H:%M}: keep {lim.min_gap_minutes} min between wakeups."  # type: ignore[arg-type]
        if by != "assistant":
            return None  # the owner's own wakeups run with the budget the owner gives them
        start, end = self.week(pid, at)
        this_week = start <= now < end
        room = self.summary(pid, now).left if this_week else lim.allowance
        planned = self.scheduled(pid, max(now, start), end, exclude_id)
        if planned + budget > room + 1e-6:
            return (f"Not enough budget that week: {room:.0f} {UNIT} left, {planned:.0f} already planned, this asks for {budget:g}."
                    if this_week else f"That week's allowance is {room:.0f} {UNIT}; {planned:.0f} already planned, this asks for {budget:g}.")
        return None

    def check_text(self, pid: str) -> str:
        """What `assistant budget` and `assistant wake plan --check` show."""
        now = self.now()
        b = self.summary(pid, now)
        eff = self.effective(pid, now)
        lim = eff.limits
        boost = ""
        if eff.boost:
            boost = f" (Boost until {self.local(parse(eff.boost['until'])):%a %H:%M}; your level is {eff.base_level.capitalize()})"  # type: ignore[arg-type]
        limits = (f"allowed hours {hours_text(lim.allowed_hours)}; at most {lim.max_per_day}/day, {lim.min_gap_minutes} min apart, "
                  f"{lim.max_wakeup:g} {UNIT} each; tiers {', '.join(lim.tiers) or 'none'}")
        lines = [f"{b.label}: {b.left:.0f} of {b.allowance:.0f} {UNIT} left this week (resets {self.local(b.resets_at):%a %H:%M}); {b.native}.",
                 f"Level {eff.level.capitalize()}{boost}: {limits}.",
                 f"Reply budget: {b.reply_left:.0f} of {b.reply_allowance:.0f} {UNIT} left.",
                 f"Pace: {b.pace}."]
        if b.room_below_reserve is not None and b.room_below_reserve < lim.allowance - b.used_by_assistant:
            lines.append(f"Your allowance is capped by the owner's reserve: {b.room_below_reserve:.0f} {UNIT} of room below {self.settings.get('RESERVE_WEEKLY_PCT'):.0f}% weekly usage.")
        if lim.sweep and pid == SUBSCRIPTION:
            lines.append(f"Sweep is on: in the last {self.settings.get('SWEEP_WINDOW_HOURS'):g} h before the reset, control adds sweep wakeups "
                         f"up to {self.settings.get('SWEEP_CEILING_PCT'):.0f}% weekly usage.")
        rows = self.db.q("SELECT * FROM wakeups WHERE provider=? AND status='planned' ORDER BY at", (pid,))
        if rows:
            lines.append("\nPlanned:")
            prev = None
            for r in rows:
                at = parse(r["at"])
                gap = f"  (gap {(at - prev).total_seconds() / 3600:.0f} h)" if prev and (at - prev) > timedelta(hours=36) else ""  # type: ignore[operator]
                lines.append(f"  #{r['id']} {self.local(at):%a %b %d %H:%M} · {r['budget']:g} {UNIT} · {r['tier']} · {r['kind']}: {r['reason']}{gap}")  # type: ignore[arg-type]
                prev = at
        else:
            lines.append("\nNothing planned." + (" Control adds a fallback wakeup if nothing is planned for 2 nights." if lim.fallback else ""))
        return "\n".join(lines)

    # ── roles, the reviewer, standing instructions (JSON in kv) ────────────
    def _kv(self, key: str) -> Any:
        row = self.db.q1("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    def _kv_set(self, key: str, value: Any) -> None:
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), self.db.now()))

    ROLE_NOTES = {"worker": "Wakeups and routines. Allowed tiers come from your level.", "reviewer": "Watches wakeups (Settings → Reviewer).",
                  "replier": "Reply-now runs.", "chat": "Your 1:1 chats (M6).", "utility": "Titles, summaries and the 'now' sentence fallback (later)."}

    def roles(self) -> list[s.RoleConfig]:
        saved = self._kv("roles") or {}
        defaults = {"worker": (SUBSCRIPTION, self.settings.get("DEFAULT_TIER")), "reviewer": (SUBSCRIPTION, "opus/medium"),
                    "replier": (SUBSCRIPTION, self.settings.get("REPLY_TIER")), "chat": (SUBSCRIPTION, "sonnet/medium"),
                    "utility": (SUBSCRIPTION, "haiku/low")}
        out = []
        for name, (prov, tier) in defaults.items():
            r = saved.get(name) or {}
            pid = r.get("provider") or prov
            if pid not in self.provider_ids():
                pid = SUBSCRIPTION
            allowed = self.effective(pid).limits.tiers if name == "worker" else (r.get("allowed") or list(TIERS))
            out.append(s.RoleConfig(role=name, provider=pid, tier=r.get("tier") or tier, allowed=allowed, note=self.ROLE_NOTES[name]))  # type: ignore[arg-type]
        return out

    def role(self, name: str) -> s.RoleConfig:
        return next(r for r in self.roles() if r.role == name)

    def set_roles(self, roles: list[s.RoleConfig]) -> None:
        saved = self._kv("roles") or {}
        for r in roles:
            if r.provider not in self.provider_ids(enabled_only=False):
                raise BadRequest("bad_provider", f"Unknown provider {r.provider}.")
            if r.tier not in TIERS and "/" not in r.tier:
                raise BadRequest("bad_tier", f"Tiers look like model/effort, e.g. sonnet/medium. Known: {', '.join(TIERS)}.")
            saved[r.role] = {"provider": r.provider, "tier": r.tier, "allowed": r.allowed}
        self._kv_set("roles", saved)

    def reviewer_config(self) -> s.ReviewerConfig:
        return s.ReviewerConfig(**(self._kv("reviewer") or {}))

    def set_reviewer_config(self, cfg: s.ReviewerConfig) -> None:
        if not 0 < cfg.budget_share <= 1:
            raise BadRequest("bad_share", "The budget share is a fraction between 0 and 1 (0.25 = a quarter of a wakeup's budget).")
        if cfg.interval_minutes < 1:
            raise BadRequest("bad_interval", "The watch interval is at least a minute.")
        self._kv_set("reviewer", cfg.model_dump())

    def instructions(self) -> s.StandingInstructions:
        row = self.db.q1("SELECT value, updated_at FROM kv WHERE key='standing_instructions'")
        return s.StandingInstructions(text=json.loads(row["value"]) if row else "", updated_at=parse(row["updated_at"]) if row else None)

    def set_instructions(self, text: str) -> None:
        self._kv_set("standing_instructions", text.strip())
