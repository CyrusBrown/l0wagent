"""Routines: scheduled prompts with their own tier and budget.

- **Built in**: the weekly review, a few hours before a Claude subscription's weekly reset. It
  exists from the start and is on only while the subscription is set up (the only provider
  with a weekly reset).
- **The owner** creates, edits, pauses and deletes routines (Settings or the Calendar).
- **The agent proposes** one with `assistant routine propose`: that files a permission card
  whose action is the routine. Approving it makes the routine active (control applies it),
  since a routine adds spend; denying it declines the proposal.

The scheduler puts each active routine's next occurrence on the plan; its run is a
'routine' run whose brief carries the routine's prompt.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .api import schemas as s
from .auth import parse
from .backends.base import BadRequest, NotFound
from .budgets import SUBSCRIPTION, Budgets
from .db import Database

if TYPE_CHECKING:
    from .scheduler import Scheduler

PROMPTS = Path(__file__).parent / "prompts"
WEEKLY_REVIEW = "weekly_review"


def check_schedule(sch: dict) -> dict:
    kind = sch.get("kind")
    try:
        if kind == "weekly":
            day = int(sch.get("day", -1))
            if not 0 <= day <= 6:
                raise ValueError
            _hhmm(sch.get("time"))
            return {"kind": "weekly", "day": day, "time": sch["time"]}
        if kind == "daily":
            _hhmm(sch.get("time"))
            return {"kind": "daily", "time": sch["time"]}
        if kind == "before_reset":
            hours = float(sch.get("hours", 6))
            if not 0.5 <= hours <= 72:
                raise ValueError
            return {"kind": "before_reset", "hours": hours}
    except (TypeError, ValueError, KeyError):
        pass
    raise BadRequest("bad_schedule", 'Schedules look like {"kind": "weekly", "day": 0-6 (Monday = 0), "time": "HH:MM"}, '
                                     '{"kind": "daily", "time": "HH:MM"} or {"kind": "before_reset", "hours": 6}.')


def _hhmm(v) -> None:
    h, m = (int(x) for x in str(v).split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError


class Routines:
    def __init__(self, db: Database, budgets: Budgets):
        self.db, self.budgets = db, budgets
        self.scheduler: Scheduler | None = None

    def ensure_builtin(self) -> None:
        if self.db.q1("SELECT 1 FROM routines WHERE builtin=?", (WEEKLY_REVIEW,)):
            return
        now = self.db.now()
        prompt = (PROMPTS / "routines" / "weekly_review.md").read_text()
        self.db.ex("INSERT INTO routines(name, prompt, schedule_json, provider, tier, budget, enabled, builtin, status, created_by, created_at, updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("Weekly review", prompt, json.dumps({"kind": "before_reset", "hours": 6}), SUBSCRIPTION, "opus/medium", 40.0, 1, WEEKLY_REVIEW, "active", "control", now, now))

    def model(self, r: dict) -> s.Routine:
        nxt = self.db.q1("SELECT at FROM wakeups WHERE routine_id=? AND status='planned' ORDER BY at LIMIT 1", (r["id"],))
        sch = json.loads(r["schedule_json"])
        from .scheduler import schedule_text
        return s.Routine(id=r["id"], name=r["name"], prompt=r["prompt"], schedule=sch, schedule_text=schedule_text(sch), provider=r["provider"], tier=r["tier"],
                         budget=r["budget"], enabled=bool(r["enabled"]), builtin=r["builtin"], status=r["status"], created_by=r["created_by"],  # type: ignore[arg-type]
                         next_at=parse(nxt["at"]) if nxt else None, last_run_at=parse(r["last_run_at"]))

    def list(self, include_declined: bool = False) -> list[s.Routine]:
        rows = self.db.q("SELECT * FROM routines WHERE (? OR status != 'declined') ORDER BY builtin IS NULL, id", (int(include_declined),))
        return [self.model(r) for r in rows]

    def get(self, rid: int) -> s.Routine:
        r = self.db.q1("SELECT * FROM routines WHERE id=?", (rid,))
        if not r:
            raise NotFound(f"routine {rid}")
        return self.model(r)

    def _check(self, tier: str, provider: str) -> None:
        if provider not in self.budgets.provider_ids(enabled_only=False):
            raise BadRequest("bad_provider", f"Unknown provider {provider}.")
        opts = self.budgets.model_options(provider)
        if not self.budgets.tier_allowed(provider, tier, opts):
            raise BadRequest("bad_tier", f"Unknown model {tier}. Known: {', '.join(opts)}.")

    def create(self, req: s.RoutineCreate, by: str = "owner", status: str = "active", run_id: int | None = None) -> s.Routine:
        provider = req.provider or self.budgets.role("worker").provider
        self._check(req.tier, provider)
        sch = check_schedule(req.schedule)
        now = self.db.now()
        rid = self.db.ex("INSERT INTO routines(name, prompt, schedule_json, provider, tier, budget, enabled, status, created_by, run_id, created_at, updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (req.name.strip(), req.prompt.strip(), json.dumps(sch), provider, req.tier, req.budget, int(req.enabled), status, by, run_id, now, now))
        if status == "active" and self.scheduler:
            self.scheduler.replan_routine(rid)
        return self.get(rid)

    def patch(self, rid: int, req: s.RoutinePatch) -> s.Routine:
        r = self.get(rid)
        fields: dict = {}
        if req.name is not None:
            fields["name"] = req.name.strip()
        if req.prompt is not None:
            fields["prompt"] = req.prompt.strip()
        if req.schedule is not None:
            fields["schedule_json"] = json.dumps(check_schedule(req.schedule))
        if req.tier is not None:
            self._check(req.tier, r.provider)
            fields["tier"] = req.tier
        if req.budget is not None:
            fields["budget"] = req.budget
        if req.enabled is not None:
            fields["enabled"] = int(req.enabled)
        if fields:
            fields["updated_at"] = self.db.now()
            self.db.ex(f"UPDATE routines SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), rid))
            if self.scheduler:
                self.scheduler.replan_routine(rid)
        return self.get(rid)

    def delete(self, rid: int) -> None:
        r = self.get(rid)
        if r.builtin:
            raise BadRequest("builtin", "The built-in weekly review can be paused, not deleted.")
        self.db.ex("UPDATE wakeups SET status='cancelled', status_reason='routine deleted', updated_at=? WHERE routine_id=? AND status='planned'", (self.db.now(), rid))
        self.db.ex("UPDATE routines SET status='declined', enabled=0, updated_at=? WHERE id=?", (self.db.now(), rid))

    # ── proposals (permission action 'routine') ────────────────────────────
    def propose_payload(self, req: s.RoutineCreate) -> dict:
        provider = req.provider or self.budgets.role("worker").provider
        self._check(req.tier, provider)
        sch = check_schedule(req.schedule)
        from .scheduler import schedule_text
        return {"name": req.name.strip(), "prompt": req.prompt.strip(), "schedule": sch, "schedule_text": schedule_text(sch), "tier": req.tier,
                "budget": req.budget, "provider": provider}

    def apply(self, payload: dict, run_id: int | None) -> dict:
        """Approving the proposal: the routine becomes active."""
        r = self.create(s.RoutineCreate(name=payload["name"], prompt=payload["prompt"], schedule=payload["schedule"], tier=payload["tier"],
                                        budget=payload["budget"], provider=payload.get("provider")), by="assistant", run_id=run_id)
        return {"routine_id": r.id}

    def revert(self, result: dict) -> None:
        if result.get("routine_id"):
            self.delete(int(result["routine_id"]))

    def occurrences_text(self, now: datetime) -> str:
        lines = []
        for r in self.list():
            if r.status == "active" and r.enabled:
                nxt = f", next {self.budgets.local(r.next_at):%a %b %d %H:%M}" if r.next_at else ""
                lines.append(f"- #{r.id} {r.name}: {r.schedule_text}, {r.tier}, {r.budget:g} pts{nxt}")
        return "\n".join(lines)
