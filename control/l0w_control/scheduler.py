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
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from . import pause as pausing
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
                    created_by=r["created_by"], routine_id=r["routine_id"], status=r["status"], status_reason=r["status_reason"], run_id=r["run_id"], used=used,
                    note=r["note"])


def schedule_text(sch: dict, unit: str = "week") -> str:
    k = sch.get("kind")
    if k == "weekly":
        return f"{DAYS[int(sch.get('day', 0)) % 7]}s at {sch.get('time', '09:00')}"
    if k == "daily":
        return f"Daily at {sch.get('time', '09:00')}"
    if k == "before_reset":
        return f"{sch.get('hours', 6):g} h before the {unit}ly reset"
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
        p = pausing.raw(self.db)
        if p and pausing.expired(p, now):
            self.end_pause(now, by="time")
        elif p:
            # Paused: nothing is planned or started; what falls due is skipped, not saved up.
            self._step("paused", self.skip_paused, now)
            for job in self.housekeeping:
                await self._step_async(getattr(job, "__name__", "housekeeping"), job, now)
            return
        # Each step on its own, so one bad routine can't stop wakeups from starting.
        for name, step in (("routines", self.routines_to_plan), ("fallback", self.fallback), ("sweep", self.sweep)):
            self._step(name, step, now)
        await self._step_async("start_due", self.start_due, now)
        for job in self.housekeeping:
            await self._step_async(getattr(job, "__name__", "housekeeping"), job, now)

    def _step(self, name: str, fn, now: datetime) -> None:
        try:
            fn(now)
        except Exception as e:  # noqa: BLE001
            self._failed(name, e)
        else:
            self._healed(name)

    async def _step_async(self, name: str, fn, now: datetime) -> None:
        try:
            await fn(now)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._failed(name, e)
        else:
            self._healed(name)

    def _failed(self, name: str, e: Exception) -> None:
        log.exception("scheduler step %s failed", name)
        try:
            h = self.problems_raw()
            prev = h.get(name) or {}
            h[name] = {"step": name, "error": f"{type(e).__name__}: {e}"[:500], "count": int(prev.get("count", 0)) + 1, "last_at": self.db.now()}
            self._store_problems(h)
        except Exception:  # noqa: BLE001
            log.exception("couldn't record the failure of %s", name)

    def _healed(self, name: str) -> None:
        h = self.problems_raw()
        if name in h:
            del h[name]
            self._store_problems(h)

    def problems_raw(self) -> dict:
        row = self.db.q1("SELECT value FROM kv WHERE key='scheduler_problems'")
        try:
            return json.loads(row["value"]) if row else {}
        except ValueError:
            return {}

    def _store_problems(self, h: dict) -> None:
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('scheduler_problems', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (json.dumps(h), self.db.now()))
        self.live.emit("status")

    def _changed(self, wid: int | None = None) -> None:
        self.live.emit("calendar", {"id": wid} if wid else {})
        self.live.emit("status")

    def _insert(self, pid: str, at: datetime, budget: float, tier: str, reason: str, kind: str, by: str, routine_id: int | None = None,
                note: str | None = None) -> int:
        now = self.db.now()
        wid = self.db.ex("INSERT INTO wakeups(provider, at, budget, tier, reason, kind, created_by, routine_id, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, iso(at), round(budget, 1), tier, reason, kind, by, routine_id, note or None, now, now))
        self._changed(wid)
        return wid

    def _note(self, text: str) -> None:
        """Tell the agent (delivered at its next inbox check or brief)."""
        self.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(NULL, ?, ?)", (f"[control] {text}", self.db.now()))

    def _set(self, wid: int, status: str, reason: str | None = None, run_id: int | None = None) -> None:
        self.db.ex("UPDATE wakeups SET status=?, status_reason=COALESCE(?, status_reason), run_id=COALESCE(?, run_id), updated_at=? WHERE id=?",
                   (status, reason, run_id, self.db.now(), wid))
        self._changed(wid)

    # ── the emergency pause ────────────────────────────────────────────────
    def skip_paused(self, now: datetime) -> None:
        for w in self.db.q("SELECT id FROM wakeups WHERE status='planned' AND at<=? ORDER BY at, id", (iso(now),)):
            self._set(w["id"], "skipped", "Everything was paused")
            pausing.add_skipped(self.db, w["id"])

    def end_pause(self, now: datetime, by: str) -> None:
        """End the pause (the owner resumed, or its time ran out) and tell the agent what it missed."""
        p = pausing.clear(self.db)
        if not p:
            return
        since = parse(p.get("since")) or now
        ids = p.get("skipped", [])
        how = "they resumed it" if by == "owner" else "it ended at the time they set"
        text = f"The owner paused everything from {self.budgets.local(since):%a %b %d %H:%M} to {self.budgets.local(now):%a %b %d %H:%M} ({how})."
        if ids:
            rows = self.db.q(f"SELECT id, at, reason, kind FROM wakeups WHERE id IN ({','.join('?' * len(ids))}) ORDER BY at", tuple(ids))
            lines = [f"#{r['id']} {self.budgets.local(parse(r['at'])):%a %H:%M} ({r['kind']}): {r['reason']}" for r in rows[:10]]  # type: ignore[arg-type]
            more = f"; and {len(rows) - 10} more" if len(rows) > 10 else ""
            text += " Skipped while paused: " + "; ".join(lines) + more + ". Re-plan what still matters (`assistant budget`)."
        else:
            text += " Nothing of yours was skipped."
        self._note(text)
        self._changed()

    # ── routines ───────────────────────────────────────────────────────────
    def next_occurrence(self, sch: dict, provider: str, after: datetime) -> datetime | None:
        tz = self.budgets.tz()
        loc = after.astimezone(tz)
        kind = sch.get("kind")
        if kind in ("weekly", "daily"):
            h, m = (int(x) for x in str(sch.get("time", "09:00")).split(":"))
            for d in range(0, 8):
                day = (loc + timedelta(days=d)).date()
                cand = datetime.combine(day, time(h % 24, m), tz).astimezone(UTC).astimezone(tz)   # a skipped hour lands after the gap
                if cand <= after:
                    continue
                if kind == "weekly" and cand.weekday() != int(sch.get("day", 0)) % 7:
                    continue
                return cand
            return None
        if kind == "before_reset":
            start, end = self.budgets.week(provider, after)
            cand = end - timedelta(hours=float(sch.get("hours", 6)))
            if cand > after:
                return cand
            _, end = self.budgets.week(provider, end + timedelta(seconds=1))   # the next period (a month isn't 7 days)
            return end - timedelta(hours=float(sch.get("hours", 6)))
        return None

    def routines_to_plan(self, now: datetime) -> None:
        enabled = set(self.budgets.provider_ids())
        horizon = now + timedelta(days=PLAN_HORIZON_DAYS)
        for r in self.db.q("SELECT * FROM routines WHERE status='active' AND enabled=1"):
            if r["builtin"]:
                r = self._follow_worker(r)
            if r["provider"] not in enabled:
                continue
            if self.db.q1("SELECT 1 FROM wakeups WHERE routine_id=? AND status IN ('planned','running') LIMIT 1", (r["id"],)):
                continue
            last = parse(r["last_run_at"]) or now - timedelta(hours=1)
            at = self.next_occurrence(json.loads(r["schedule_json"]), r["provider"], max(now, last + timedelta(hours=1)))  # type: ignore[operator]
            reason = f"Routine: {r['name']}"
            if at:
                moved = self._clear_of_others(r["provider"], at, r["id"])
                if moved and moved != at:
                    reason += f" (moved from {self.budgets.local(at):%a %H:%M}: another wakeup is within {self._gap_minutes(r['provider'], now):g} min)"
                    self._note(f"Routine \"{r['name']}\" was moved from {self.budgets.local(at):%a %H:%M} to {self.budgets.local(moved):%a %H:%M} "
                               "because another wakeup was too close.")
                at = moved
            if at and at <= horizon:
                self._insert(r["provider"], at, r["budget"], r["tier"], reason, "routine", "control", r["id"])

    def _gap_minutes(self, pid: str, now: datetime) -> float:
        return self.budgets.effective(pid, now).limits.min_gap_minutes

    def _clear_of_others(self, pid: str, at: datetime, routine_id: int) -> datetime:
        """`at`, or the first time after it that keeps the level's minimum gap from every other wakeup."""
        gap = timedelta(minutes=self._gap_minutes(pid, at))
        if gap <= timedelta(0):
            return at
        for _ in range(20):
            near = self.db.q1("SELECT at FROM wakeups WHERE provider=? AND status IN ('planned','running') AND kind != 'sweep' "
                              "AND routine_id IS NOT ? AND at>? AND at<? ORDER BY at DESC LIMIT 1", (pid, routine_id, iso(at - gap), iso(at + gap)))
            if not near:
                return at
            at = parse(near["at"]) + gap  # type: ignore[operator]
        return at

    def _follow_worker(self, r: dict) -> dict:
        """The built-in review runs where the worker does (its period decides when it fires)."""
        worker = self.budgets.role("worker")
        if r["provider"] == worker.provider:
            return r
        tier = "opus/medium" if worker.provider == SUBSCRIPTION else worker.tier
        self.db.ex("UPDATE wakeups SET status='cancelled', status_reason='the review follows the worker provider', updated_at=? WHERE routine_id=? AND status='planned'",
                   (self.db.now(), r["id"]))
        self.db.ex("UPDATE routines SET provider=?, tier=?, updated_at=? WHERE id=?", (worker.provider, tier, self.db.now(), r["id"]))
        return {**r, "provider": worker.provider, "tier": tier}

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
            self._insert(pid, at, budget, self._worker_tier(pid, eff, 0),
                         "Fallback: nothing was planned for the next two nights", "fallback", "control")
            key = f"fallback_noted:{pid}:{iso(self.budgets.week(pid, now)[1])}"
            if not self.db.q1("SELECT 1 FROM kv WHERE key=?", (key,)):   # once per period, however often it's replanned
                self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?, '1', ?)", (key, self.db.now()))
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
        tier = self._worker_tier(pid, eff, -1)
        self._insert(pid, at, room, tier, f"Sweep: {room:.0f} pts you didn't use this week, before it resets at {self.budgets.local(win[1]):%a %H:%M}", "sweep", "control")

    def _worker_tier(self, pid: str, eff, pick: int) -> str:
        """The worker role's tier if this level allows it, else the first (or last) allowed one."""
        tier = self.budgets.role("worker").tier
        if self.budgets.tier_allowed(pid, tier, eff.limits.tiers):
            return tier
        return (eff.limits.tiers or ["sonnet/medium"])[pick]

    # ── starting due wakeups ───────────────────────────────────────────────
    async def start_due(self, now: datetime) -> None:
        from .updates import draining
        if draining(self.db):
            return  # an update is waiting for idle: wakeups wait (and may be missed)
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
        elif kind == "owner" and eff.limits.allowance <= 0:
            bucket, budget = "allowance", w["budget"]   # Dormant: the owner's own wakeup keeps the budget the owner gave
        else:
            bucket, budget = "allowance", min(w["budget"], self.budgets.left(pid, now))
            if budget < 0.5:
                unit = self.budgets.period_unit(pid)
                self._set(w["id"], "skipped", f"This {unit}'s allowance is used up")
                if w["created_by"] == "assistant":
                    self._note(f"Your wakeup #{w['id']} ({w['reason']}) was skipped: this {unit}'s allowance is used up.")
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
    def _drop_fallbacks(self, pid: str, times: list[datetime]) -> list[int]:
        """The agent planning its own wakeups within the next 48 h replaces control's fallback
        (restored if the plan is rejected). A plan that leaves those two nights empty keeps it."""
        if not any(at < self.budgets.now() + timedelta(hours=48) for at in times):
            return []
        rows = [r["id"] for r in self.db.q("SELECT id FROM wakeups WHERE provider=? AND kind='fallback' AND status='planned'", (pid,))]
        for wid in rows:
            self.db.ex("UPDATE wakeups SET status='cancelled', status_reason='replaced by the assistant''s own plan', updated_at=? WHERE id=?", (self.db.now(), wid))
        return rows

    def _restore(self, ids: list[int]) -> None:
        for wid in ids:
            self.db.ex("UPDATE wakeups SET status='planned', status_reason=NULL, updated_at=? WHERE id=?", (self.db.now(), wid))

    def plan(self, pid: str, at: datetime, budget: float, tier: str | None, reason: str, by: str, note: str | None = None) -> s.Wakeup:
        from .backends.base import BadRequest
        tier = tier or self.budgets.role("worker").tier
        dropped = self._drop_fallbacks(pid, [at]) if by == "assistant" else []
        problem = self.budgets.validate(pid, at, budget, tier, by=by)
        if problem:
            self._restore(dropped)
            raise BadRequest("wakeup_rejected", problem)
        wid = self._insert(pid, at, budget, tier, reason, "planned" if by == "assistant" else "owner", by, note=note)
        return wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,)))  # type: ignore[arg-type]

    def replace_plan(self, pid: str, items: list[tuple[datetime, float, str | None, str, str | None]]) -> list[s.Wakeup]:
        """The agent's `wake plan --replace`: its own future wakeups become exactly these.
        All are validated first; nothing changes if one is rejected. A wakeup that keeps its
        time keeps its note unless the new item brings one."""
        from .backends.base import BadRequest
        old = self.db.q("SELECT id, at, note FROM wakeups WHERE provider=? AND status='planned' AND created_by='assistant'", (pid,))
        old_notes = {parse(r["at"]): r["note"] for r in old if r["note"]}
        old_ids = [r["id"] for r in old] + self._drop_fallbacks(pid, [i[0] for i in items])
        with self.db.tx() as db:
            for wid in old_ids:
                db.ex("UPDATE wakeups SET status='cancelled', status_reason='replaced by a new plan', updated_at=? WHERE id=?", (db.now(), wid))
        made: list[int] = []
        try:
            for i, (at, budget, tier, reason, note) in enumerate(sorted(items, key=lambda x: x[0])):
                tier = tier or self.budgets.role("worker").tier
                problem = self.budgets.validate(pid, at, budget, tier, by="assistant")
                if problem:
                    raise BadRequest("wakeup_rejected", f"Item {i + 1} ({self.budgets.local(at):%a %H:%M}): {problem}")
                made.append(self._insert(pid, at, budget, tier, reason, "planned", "assistant", note=note or old_notes.get(at)))
        except BadRequest:
            with self.db.tx() as db:
                for wid in made:
                    db.ex("DELETE FROM wakeups WHERE id=?", (wid,))
                for wid in old_ids:
                    db.ex("UPDATE wakeups SET status='planned', status_reason=NULL, updated_at=? WHERE id=?", (db.now(), wid))
            raise
        self._changed()
        return [wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (w,))) for w in made]  # type: ignore[arg-type]

    def set_note(self, wid: int, note: str | None) -> s.Wakeup:
        """The agent's `wake note ID`: only on its own wakeups that haven't started."""
        from .backends.base import BadRequest, NotFound
        w = self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,))
        if not w or w["created_by"] != "assistant":
            raise NotFound(f"wakeup {wid}")
        if w["status"] != "planned":
            raise BadRequest("not_planned", f"Wakeup #{wid} is {w['status']}; notes go on planned wakeups.")
        self.db.ex("UPDATE wakeups SET note=?, updated_at=? WHERE id=?", (note or None, self.db.now(), wid))
        self._changed(wid)
        return wakeup_model(self.db, self.db.q1("SELECT * FROM wakeups WHERE id=?", (wid,)))  # type: ignore[arg-type]

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
