"""The scheduler: turns the wakeup plan into runs.

Every tick (15 s) it:
1. **Routines**: puts each active routine's next occurrence on the plan (kind 'routine').
2. **Fallback**: if the level allows it and nothing is planned for the next 48 hours (two
   nights), it plans one night wakeup and tells the agent why, so the assistant can't strand
   itself.
3. **Sweep**: on a Claude subscription with the sweep on, in the last SWEEP_WINDOW_HOURS before
   the weekly reset and below SWEEP_CEILING_PCT, it adds a sweep wakeup that spends what the
   owner left unused. A sweep run that ends `IDLE` (nothing worthwhile) ends the sweep for
   that reset; otherwise another follows while there's room.
4. **Due wakeups**: starts the earliest one. One run at a time: a wakeup that can't start
   within WAKEUP_LATE_MINUTES of its time is marked missed, and the agent hears about it.

Budgets: planned, fallback and routine wakeups are capped by the allowance left (and skipped
when it's gone); sweep wakeups use the sweep's own room; the owner's own additions run with
the budget the owner gave. Dormant skips everything except the owner's additions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from .api import schemas as s
from .auth import iso, parse
from .backends.base import Conflict
from .budgets import PLAN_HORIZON_DAYS, SUBSCRIPTION, Budgets, in_hours, next_in_hours
from .config import Settings
from .db import Database
from .live import Live

if TYPE_CHECKING:
    from .runs.manager import RunManager

log = logging.getLogger("l0w.scheduler")
TICK_S = 15
FALLBACK_HOUR = 3
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def wakeup_model(db: Database, r: dict) -> s.Wakeup:
    used = None
    if r["run_id"]:
        run = db.q1("SELECT used FROM runs WHERE id=?", (r["run_id"],))
        used = round(run["used"] or 0, 1) if run else None
    return s.Wakeup(id=r["id"], provider=r["provider"], at=parse(r["at"]), budget=r["budget"], tier=r["tier"], reason=r["reason"], kind=r["kind"],  # type: ignore[arg-type]
                    created_by=r["created_by"], routine_id=r["routine_id"], status=r["status"], status_reason=r["status_reason"], run_id=r["run_id"], used=used)


def schedule_text(sch: dict) -> str:
    k = sch.get("kind")
    if k == "weekly":
        return f"{DAYS[int(sch.get('day', 0)) % 7]}s at {sch.get('time', '09:00')}"
    if k == "daily":
        return f"Daily at {sch.get('time', '09:00')}"
    if k == "before_reset":
        return f"{sch.get('hours', 6):g} h before the weekly reset"
    return "Unscheduled"


class Scheduler:
    def __init__(self, db: Database, settings: Settings, budgets: Budgets, runs: RunManager, live: Live):
        self.db, self.settings, self.budgets, self.runs, self.live = db, settings, budgets, runs, live
        self.housekeeping: list = []   # async (now) -> None, run each tick (promote expiry, job liveness)
        runs.on_finish.append(self._run_finished)

    # ── the loop ───────────────────────────────────────────────────────────
    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self.budgets.now()
        self.routines_to_plan(now)
        self.fallback(now)
        self.sweep(now)
        await self.start_due(now)
        for job in self.housekeeping:
            try:
                await job(now)
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")

    def _changed(self, wid: int | None = None) -> None:
        self.live.emit("calendar", {"id": wid} if wid else {})
        self.live.emit("status")

    def _insert(self, pid: str, at: datetime, budget: float, tier: str, reason: str, kind: str, by: str, routine_id: int | None = None) -> int:
        now = self.db.now()
        wid = self.db.ex("INSERT INTO wakeups(provider, at, budget, tier, reason, kind, created_by, routine_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (pid, iso(at), round(budget, 1), tier, reason, kind, by, routine_id, now, now))
        self._changed(wid)
        return wid

    def _note(self, text: str) -> None:
        """Tell the agent (delivered at its next inbox check or brief)."""
        self.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(NULL, ?, ?)", (f"[control] {text}", self.db.now()))

    def _set(self, wid: int, status: str, reason: str | None = None, run_id: int | None = None) -> None:
        self.db.ex("UPDATE wakeups SET status=?, status_reason=COALESCE(?, status_reason), run_id=COALESCE(?, run_id), updated_at=? WHERE id=?",
                   (status, reason, run_id, self.db.now(), wid))
        self._changed(wid)

    # ── routines ───────────────────────────────────────────────────────────
    def next_occurrence(self, sch: dict, provider: str, after: datetime) -> datetime | None:
        tz = self.budgets.tz()
        loc = after.astimezone(tz)
        kind = sch.get("kind")
        if kind in ("weekly", "daily"):
            h, m = (int(x) for x in str(sch.get("time", "09:00")).split(":"))
            for d in range(0, 8):
                day = (loc + timedelta(days=d)).date()
                cand = datetime.combine(day, time(h % 24, m), tz)
                if cand <= after:
                    continue
                if kind == "weekly" and cand.weekday() != int(sch.get("day", 0)) % 7:
                    continue
                return cand
            return None
        if kind == "before_reset":
            start, end = self.budgets.week(provider, after)
            cand = end - timedelta(hours=float(sch.get("hours", 6)))
            return cand if cand > after else cand + timedelta(days=7)
        return None

    def routines_to_plan(self, now: datetime) -> None:
        enabled = set(self.budgets.provider_ids())
        horizon = now + timedelta(days=PLAN_HORIZON_DAYS)
        for r in self.db.q("SELECT * FROM routines WHERE status='active' AND enabled=1"):
            if r["provider"] not in enabled:
                continue
            if self.db.q1("SELECT 1 FROM wakeups WHERE routine_id=? AND status IN ('planned','running') LIMIT 1", (r["id"],)):
                continue
            last = parse(r["last_run_at"]) or now - timedelta(hours=1)
            at = self.next_occurrence(json.loads(r["schedule_json"]), r["provider"], max(now, last + timedelta(hours=1)))  # type: ignore[operator]
            if at and at <= horizon:
                self._insert(r["provider"], at, r["budget"], r["tier"], f"Routine: {r['name']}", "routine", "control", r["id"])

    def replan_routine(self, routine_id: int) -> None:
        self.db.ex("UPDATE wakeups SET status='cancelled', status_reason='the routine changed', updated_at=? WHERE routine_id=? AND status='planned'",
                   (self.db.now(), routine_id))
        self.routines_to_plan(self.budgets.now())
        self._changed()

    # ── fallback ───────────────────────────────────────────────────────────
    def fallback(self, now: datetime) -> None:
        for pid in self._worker_providers():
            eff = self.budgets.effective(pid, now)
            if not eff.limits.fallback or eff.limits.allowance <= 0:
                continue
            soon = self.db.q1("SELECT 1 FROM wakeups WHERE provider=? AND status IN ('planned','running') AND kind != 'sweep' AND at<? LIMIT 1",
                              (pid, iso(now + timedelta(hours=48))))
            if soon:
                continue
            budget = min(self.budgets.fallback_pts(pid, now), self.budgets.left(pid, now))
            if budget < 1:
                continue
            at = self._night_slot(eff.limits.allowed_hours, now)
            if not at:
                continue
            self._insert(pid, at, budget, self.budgets.role("worker").tier if self.budgets.role("worker").tier in eff.limits.tiers else (eff.limits.tiers or ["sonnet/medium"])[0],
                         "Fallback: nothing was planned for the next two nights", "fallback", "control")
            self._note(f"Nothing was planned for the next two nights, so control planned a fallback wakeup at {self.budgets.local(at):%a %H:%M}. "
                       "Plan your own wakeups with `assistant wake at …` (see `assistant budget`).")

    def _night_slot(self, windows: list[dict], now: datetime) -> datetime | None:
        tz = self.budgets.tz()
        loc = now.astimezone(tz)
        target = datetime.combine(loc.date(), time(FALLBACK_HOUR), tz)
        while target < now + timedelta(hours=1):
            target += timedelta(days=1)
        if in_hours(windows, target):
            return target
        return next_in_hours(windows, target) or next_in_hours(windows, loc + timedelta(hours=1))

    def _worker_providers(self) -> list[str]:
        worker = self.budgets.role("worker").provider
        return [p for p in self.budgets.provider_ids() if p == worker]

    # ── sweep ──────────────────────────────────────────────────────────────
    def sweep_window(self, pid: str, now: datetime) -> tuple[datetime, datetime] | None:
        if pid != SUBSCRIPTION:
            return None
        u = self.budgets.usage.cached()
        if not u or not u.seven_day_resets_at:
            return None
        _, end = self.budgets.week(pid, now)
        return end - timedelta(hours=self.settings.get("SWEEP_WINDOW_HOURS")), end

    def sweep(self, now: datetime) -> None:
        pid = SUBSCRIPTION
        if pid not in self._worker_providers():
            return
        eff = self.budgets.effective(pid, now)
        if not eff.limits.sweep:
            return
        win = self.sweep_window(pid, now)
        if not win or not (win[0] <= now < win[1]):
            return
        u = self.budgets.usage.cached()
        ceiling = self.settings.get("SWEEP_CEILING_PCT")
        if not u or u.seven_day_pct is None or u.seven_day_pct >= ceiling - 0.5:
            return
        if self.db.q1("SELECT 1 FROM kv WHERE key=?", (f"sweep_done:{iso(win[1])}",)):
            return
        if self.db.q1("SELECT 1 FROM wakeups WHERE provider=? AND kind='sweep' AND status IN ('planned','running') LIMIT 1", (pid,)):
            return
        at = now if in_hours(eff.limits.allowed_hours, self.budgets.local(now)) else next_in_hours(eff.limits.allowed_hours, self.budgets.local(now))
        if not at or at >= win[1]:
            return
        room = (ceiling - u.seven_day_pct) * 10
        tier = self.budgets.role("worker").tier if self.budgets.role("worker").tier in eff.limits.tiers else (eff.limits.tiers or ["sonnet/medium"])[-1]
        self._insert(pid, at, room, tier, f"Sweep: {room:.0f} pts you didn't use this week, before it resets at {self.budgets.local(win[1]):%a %H:%M}", "sweep", "control")

    # ── starting due wakeups ───────────────────────────────────────────────
    async def start_due(self, now: datetime) -> None:
        due = self.db.q("SELECT * FROM wakeups WHERE status='planned' AND at<=? ORDER BY at, id", (iso(now),))
        late = timedelta(minutes=self.settings.get("WAKEUP_LATE_MINUTES"))
        busy = self.runs.active_run_id() is not None
        for w in due:
            at = parse(w["at"])
            if now - at > late:  # type: ignore[operator]
                why = "another run was still going" if busy else "control wasn't running at the time"
                self._set(w["id"], "missed", f"Didn't start within {late.total_seconds() / 60:.0f} min: {why}")
                if w["created_by"] == "assistant" or w["kind"] in ("fallback", "routine"):
                    self._note(f"Your wakeup #{w['id']} planned for {self.budgets.local(at):%a %H:%M} ({w['reason']}) was missed: {why}. Re-plan if it still matters.")  # type: ignore[arg-type]
                continue
            if busy:
                return
            await self._start(w, now)
            return

    async def _start(self, w: dict, now: datetime) -> None:
        pid = w["provider"]
        eff = self.budgets.effective(pid, now)
        kind = w["kind"]
        if eff.limits.allowance <= 0 and kind != "owner":
            self._set(w["id"], "skipped", "The level is Dormant")
            return
        if kind == "sweep":
            bucket, budget = "sweep", w["budget"]
        elif kind == "owner":
            bucket, budget = "allowance", w["budget"]
        else:
            bucket, budget = "allowance", min(w["budget"], self.budgets.left(pid, now))
            if budget < 0.5:
                self._set(w["id"], "skipped", "This week's allowance is used up")
                if w["created_by"] == "assistant":
                    self._note(f"Your wakeup #{w['id']} ({w['reason']}) was skipped: this week's allowance is used up.")
                return
        routine = self.db.q1("SELECT * FROM routines WHERE id=?", (w["routine_id"],)) if w["routine_id"] else None
        label = {"planned": "Planned wakeup", "fallback": "Fallback wakeup", "sweep": "Sweep wakeup", "routine": "Routine", "owner": "Your wakeup"}[kind]
        try:
            run = await self.runs.start(s.RunStart(budget=budget, tier=w["tier"], reason=w["reason"]), kind="routine" if routine else "wakeup",
                                        why=kind, provider=pid, bucket=bucket, wakeup_id=w["id"], routine_id=routine and routine["id"], label=label)
        except Conflict as e:
            if e.error == "busy":
                return
            self._set(w["id"], "skipped", e.detail or e.error)
            return
        self._set(w["id"], "running", None, run.id)
        if routine:
            self.db.ex("UPDATE routines SET last_run_at=? WHERE id=?", (self.db.now(), routine["id"]))

    def _run_finished(self, run: dict) -> None:
        if run.get("wakeup_id"):
            w = self.db.q1("SELECT * FROM wakeups WHERE id=?", (run["wakeup_id"],))
            if w:
                self._set(w["id"], "done", f"{run['status']} · used {run['used'] or 0:.1f} pts")
                if w["kind"] == "sweep" and (run.get("idle") or run["status"] != "done"):
                    win = self.sweep_window(w["provider"], self.budgets.now())
                    if win:
                        self.db.ex("INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES(?,?,?)", (f"sweep_done:{iso(win[1])}", "1", self.db.now()))
        self._changed()

    # ── planning API (agent and owner) ─────────────────────────────────────
    def _drop_fallbacks(self, pid: str) -> list[int]:
        """The agent planning its own wakeups replaces control's fallback (restored if the plan is rejected)."""
        rows = [r["id"] for r in self.db.q("SELECT id FROM wakeups WHERE provider=? AND kind='fallback' AND status='planned'", (pid,))]
        for wid in rows:
            self.db.ex("UPDATE wakeups SET status='cancelled', status_reason='replaced by the assistant''s own plan', updated_at=? WHERE id=?", (self.db.now(), wid))
        return rows

    def _restore(self, ids: list[int]) -> None:
        for wid in ids:
            self.db.ex("UPDATE wakeups SET status='planned', status_reason=NULL, updated_at=? WHERE id=?", (self.db.now(), wid))

    def plan(self, pid: str, at: datetime, budget: float, tier: str | None, reason: str, by: str) -> s.Wakeup:
        from .backends.base import BadRequest
        tier = tier or self.budgets.role("worker").tier
        dropped = self._drop_fallbacks(pid) if by == "assistant" else []
        problem = self.budgets.validate(pid, at, budget, tier, by=by)
        if problem:
            self._restore(dropped)
            raise BadRequest("wakeup_rejected", problem)
        wid = self._insert(pid, at, budget, tier, reason, "planned" if by == "assistant" else "owner", by)
        return wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,)))  # type: ignore[arg-type]

    def replace_plan(self, pid: str, items: list[tuple[datetime, float, str | None, str]]) -> list[s.Wakeup]:
        """The agent's `wake plan --replace`: its own future wakeups become exactly these.
        All are validated first; nothing changes if one is rejected."""
        from .backends.base import BadRequest
        old = self.db.q("SELECT id FROM wakeups WHERE provider=? AND status='planned' AND created_by='assistant'", (pid,))
        old_ids = [r["id"] for r in old] + self._drop_fallbacks(pid)
        with self.db.tx() as db:
            for wid in old_ids:
                db.ex("UPDATE wakeups SET status='cancelled', status_reason='replaced by a new plan', updated_at=? WHERE id=?", (db.now(), wid))
        made: list[int] = []
        try:
            for i, (at, budget, tier, reason) in enumerate(sorted(items, key=lambda x: x[0])):
                tier = tier or self.budgets.role("worker").tier
                problem = self.budgets.validate(pid, at, budget, tier, by="assistant")
                if problem:
                    raise BadRequest("wakeup_rejected", f"Item {i + 1} ({self.budgets.local(at):%a %H:%M}): {problem}")
                made.append(self._insert(pid, at, budget, tier, reason, "planned", "assistant"))
        except BadRequest:
            with self.db.tx() as db:
                for wid in made:
                    db.ex("DELETE FROM wakeups WHERE id=?", (wid,))
                for wid in old_ids:
                    db.ex("UPDATE wakeups SET status='planned', status_reason=NULL, updated_at=? WHERE id=?", (db.now(), wid))
            raise
        self._changed()
        return [wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (w,))) for w in made]  # type: ignore[arg-type]

    def cancel(self, wid: int, by: str) -> s.Wakeup:
        from .backends.base import BadRequest, NotFound
        w = self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,))
        if not w:
            raise NotFound(f"wakeup {wid}")
        if w["status"] != "planned":
            raise BadRequest("not_planned", f"Wakeup #{wid} is {w['status']}, not planned.")
        if by == "assistant" and w["created_by"] == "owner":
            raise BadRequest("owners", "That wakeup is the owner's; only they can change it.")
        self._set(wid, "cancelled", f"cancelled by {'you' if by == 'owner' else 'the assistant'}")
        return wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,)))  # type: ignore[arg-type]

    def owner_edit(self, wid: int, req: s.WakeupPatch) -> s.Wakeup:
        from .backends.base import BadRequest, NotFound
        w = self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,))
        if not w:
            raise NotFound(f"wakeup {wid}")
        if w["status"] != "planned":
            raise BadRequest("not_planned", f"Wakeup #{wid} is {w['status']}, not planned.")
        at = req.at or parse(w["at"])
        budget = req.budget if req.budget is not None else w["budget"]
        tier = req.tier or w["tier"]
        problem = self.budgets.validate(w["provider"], at, budget, tier, exclude_id=wid, by="owner")  # type: ignore[arg-type]
        if problem:
            raise BadRequest("wakeup_rejected", problem)
        self.db.ex("UPDATE wakeups SET at=?, budget=?, tier=?, reason=?, updated_at=? WHERE id=?",
                   (iso(at), budget, tier, req.reason or w["reason"], self.db.now(), wid))  # type: ignore[arg-type]
        if w["created_by"] == "assistant" and (req.at or req.budget is not None):
            self._note(f"The owner changed your wakeup #{wid} ({w['reason']}): now {self.budgets.local(at):%a %H:%M}, {budget:g} pts.")  # type: ignore[arg-type]
        self._changed(wid)
        return wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,)))  # type: ignore[arg-type]

    def next_wakeup(self) -> s.Wakeup | None:
        r = self.db.q1("SELECT * FROM wakeups WHERE status='planned' ORDER BY at LIMIT 1")
        return wakeup_model(self.db, r) if r else None
