"""Runs: starting them, following them, and keeping them alive across restarts.

A run holds one or more sessions. Each session runs in the runner (inside the agent
container), which appends its events to a log. Control follows the log by byte offset,
storing each event and its timeline items in one transaction with the new offset, so:

- **Control restarts**: on startup it re-attaches to every unfinished run from the stored
  offset (no event lost or stored twice), and re-acquires the egress lease.
- **The agent container restarts**: the runner marks the cut-off session `lost`; control
  resumes the conversation (`--resume`) once the runner is healthy, telling the agent it
  was interrupted, up to RUN_MAX_RESUMES times.
- **Control is down**: nothing supervises the session, but the egress lease expires
  LEASE_TTL seconds after the last renewal, so the agent can't keep calling the model.

Stopping escalates: SIGINT (end the turn) → after STOP_GRACE_SECONDS, kill → then revoke
the lease. The last step holds even if the agent (root in its container) tampers with
the runner.

Budgets (M4): every run has a provider, a budget in points and a bucket it spends from
(the allowance, the reply budget, or the sweep). Each model message's tokens go into the
usage ledger as they arrive (budgets.py), so `used` is live and the monitor stops the run
at its budget. A subscription's weekly-meter delta over the run calibrates points per
API-dollar at the end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..api import schemas as s
from ..auth import iso, parse, utcnow
from ..backends.base import Conflict, NotFound
from ..budgets import SUBSCRIPTION, UNIT, Budgets
from ..config import Env, Settings
from ..db import Database
from ..live import Live
from .clients import EgressClient, RunnerClient, RunnerError, RunnerIdle, RunnerSettingUp
from .endings import ending, explain_error, stopped_sentence
from .runtime import ADAPTERS, Item, RuntimeAdapter
from .usage import Usage

if TYPE_CHECKING:
    from ..conv import Conversations
    from ..vault import Vault

log = logging.getLogger("l0w.runs")

LEASE_TTL = 240
LEASE_RENEW_S = 30
MONITOR_S = 5
SETUP_POLL_S = 10        # while the agent computer sets itself up, how often a waiting run checks again
SETUP_WAIT_S = 40 * 60   # and for how long at most (its provision.sh may take 30 min, boot.sh 5)
HANDOVER_S = 30          # after setup, how long the runner may take to answer on the stand-in's port
USAGE_CHECK_S = 120
MAX_EVENT_BYTES = 200_000
BRIEF_LIST_CAP = 15      # the default of the BRIEF_LIST_CAP setting (items per list section of the brief)
PROMPTS = Path(__file__).parent.parent / "prompts"
ACTIVE = ("starting", "running")
CLAIM_UNCLOSED = "The run ended before this was closed."
NUDGE_MARK = "Nudged to carry on"


def run_model(db: Database, row: dict) -> s.Run:
    projects = [r["slug"] for r in db.q(
        "SELECT DISTINCT p.slug FROM projects p JOIN threads t ON t.project_id=p.id WHERE t.id=? OR t.id IN (SELECT thread_id FROM claims WHERE run_id=?)",
        (row.get("thread_id"), row["id"]))]
    return s.Run(
        id=row["id"], kind=row["kind"], why=row["why"], why_detail=row["why_detail"], about=row["description"], now=row["now_sentence"],
        provider=row["provider"], tier=row["tier"], budget=row["budget"], used=round(row["used"] or 0, 2), deadline=parse(row["deadline"]),
        status=row["status"], started_at=parse(row["started_at"]), ended_at=parse(row["ended_at"]), projects=projects,  # type: ignore[arg-type]
        bucket=row.get("bucket") or "allowance", level=row.get("level"), reviewer_mode=row.get("reviewer_mode") or "off",  # type: ignore[arg-type]
        wakeup_id=row.get("wakeup_id"), routine_id=row.get("routine_id"), end_reason=row.get("end_reason"),
        ending=ending(row["status"], row.get("end_reason"), row.get("stop_reason"), row.get("result")),
    )


@dataclass
class Session:
    id: int                 # run_sessions.id
    runner_session: str
    offset: int
    runtime_session: str    # the runtime's id, for --resume ('' until reported)
    runtime: str = "claude-code"


class RunManager:
    def __init__(self, env: Env, db: Database, settings: Settings, live: Live, runner: RunnerClient, egress: EgressClient, usage: Usage,
                 conv: Conversations, vault: Vault, budgets: Budgets, adapter: RuntimeAdapter | None = None):
        self.env, self.db, self.settings, self.live = env, db, settings, live
        self.conv, self.vault, self.budgets = conv, vault, budgets
        self.runner, self.egress, self.usage = runner, egress, usage
        self.fixed_adapter = adapter  # tests may pin one; otherwise each run's provider picks it
        # Hooks other parts of control attach: brief sections, run endings, the reviewer.
        self.brief_extra = None      # (run row) -> str: the wakeup brief's budget/plan sections
        self.on_finish: list = []    # (run row after finishing) -> None
        self.fire = lambda event, data: None         # extensions' event hooks
        self.ext_prompt = lambda slot: ""            # extensions' prompt fragments, by slot
        self.reviewer = None         # reviewer.Reviewer, attached by the backend
        self.tasks: dict[int, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()
        self._start_lock = asyncio.Lock()

    def adapter(self, runtime: str | None) -> RuntimeAdapter:
        return self.fixed_adapter or ADAPTERS.get(runtime or "claude-code", ADAPTERS["claude-code"])

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def startup(self) -> None:
        self._spawn(self._lease_keeper())
        for row in self.db.q("SELECT id FROM runs WHERE status IN ('starting','running') ORDER BY id"):
            self.timeline(row["id"], Item("control", "Control restarted; re-attached to the run", True))
            self._drive_task(row["id"])

    async def shutdown(self) -> None:
        """Stop following runs without stopping them: they carry on in the runner."""
        loop = asyncio.get_running_loop()
        mine = [t for t in [*self.tasks.values(), *self._bg] if t.get_loop() is loop]
        for t in mine:
            t.cancel()
        await asyncio.gather(*mine, return_exceptions=True)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _drive_task(self, run_id: int) -> None:
        t = asyncio.create_task(self._drive(run_id))
        self.tasks[run_id] = t
        t.add_done_callback(lambda _: self.tasks.pop(run_id, None))

    async def _lease_keeper(self) -> None:
        while True:
            await asyncio.sleep(LEASE_RENEW_S)
            try:
                await self.egress.renew(LEASE_TTL)
            except RunnerError as e:
                log.warning("lease renewal failed: %s", e)

    # ── queries ────────────────────────────────────────────────────────────
    def active_run_id(self) -> int | None:
        row = self.db.q1("SELECT id FROM runs WHERE status IN ('starting','running') ORDER BY id DESC LIMIT 1")
        return row["id"] if row else None

    def run(self, run_id: int) -> s.Run:
        row = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise NotFound(f"run {run_id}")
        return run_model(self.db, row)

    # ── recording ──────────────────────────────────────────────────────────
    def _run_changed(self, run_id: int) -> None:
        self.live.changed("run", run_id)
        self.live.emit("status")

    def timeline(self, run_id: int, item: Item, source_event_id: int | None = None) -> int:
        tid = self.db.ex(
            "INSERT INTO timeline_items(run_id, at, kind, simple, text, data_json, source_event_id) VALUES(?,?,?,?,?,?,?)",
            (run_id, self.db.now(), item.kind, int(item.simple), self.vault.redact(item.text), self.vault.redact(json.dumps(item.data)), source_event_id),
        )
        self.live.emit("timeline", {"run_id": run_id, "id": tid})
        return tid

    def _process(self, run_id: int, sess: Session, event: dict, size: int) -> tuple[bool, dict | None]:
        """Store one log line and what it means, and advance the offset, atomically.
        Returns (run row changed, result if this was the session's result)."""
        parsed = self.adapter(sess.runtime).parse(event)
        self._neutralize_if_stopped(run_id, parsed)
        raw = self.vault.redact(json.dumps(event)) or ""
        if len(raw) > MAX_EVENT_BYTES:
            raw = json.dumps({"type": event.get("type"), "truncated": True, "preview": raw[:MAX_EVENT_BYTES]})
        changed = False
        item_ids: list[int] = []
        with self.db.tx() as db:
            ev_id = db.ex("INSERT INTO run_events(run_id, session_id, at, raw_json) VALUES(?,?,?,?)", (run_id, sess.id, db.now(), raw))
            for item in parsed.items:
                item_ids.append(db.ex(
                    "INSERT INTO timeline_items(run_id, at, kind, simple, text, data_json, source_event_id) VALUES(?,?,?,?,?,?,?)",
                    (run_id, db.now(), item.kind, int(item.simple), self.vault.redact(item.text), self.vault.redact(json.dumps(item.data)), ev_id),
                ))
            for tool_use_id, text, is_error in parsed.tool_results:
                row = db.q1("SELECT id, data_json FROM timeline_items WHERE run_id=? AND kind='tool' AND json_extract(data_json, '$.id')=? ORDER BY id DESC LIMIT 1",
                            (run_id, tool_use_id))
                if row:
                    data = json.loads(row["data_json"])
                    data.update(result=self.vault.redact(text), is_error=is_error)
                    db.ex("UPDATE timeline_items SET data_json=? WHERE id=?", (json.dumps(data), row["id"]))
                    item_ids.append(row["id"])
            if parsed.session_id and parsed.session_id != sess.runtime_session:
                sess.runtime_session = parsed.session_id
                db.ex("UPDATE run_sessions SET session_id=? WHERE id=?", (parsed.session_id, sess.id))
            if parsed.model:
                db.ex("UPDATE run_sessions SET model=? WHERE id=?", (parsed.model, sess.id))
            if parsed.runner == "started":
                db.ex("UPDATE runs SET status='running', workspace_head=COALESCE(workspace_head, ?) WHERE id=? AND status='starting'",
                      (parsed.runner_data.get("head"), run_id))
                changed = True
            if parsed.result:
                db.ex("UPDATE runs SET result=? WHERE id=?", (self.vault.redact(parsed.result["text"]), run_id))
                db.ex("UPDATE run_sessions SET result_ok=? WHERE id=?", (int(bool(parsed.result["ok"])), sess.id))
            sess.offset += size
            db.ex("UPDATE run_sessions SET log_offset=? WHERE id=?", (sess.offset, sess.id))
        if parsed.rate_limit:
            self.usage.record_rate_limit(run_id, parsed.rate_limit)
        if parsed.usage or (parsed.result and parsed.result.get("cost_usd") is not None):
            changed = self._count(run_id, sess, parsed) or changed
        for tid in item_ids:
            self.live.emit("timeline", {"run_id": run_id, "id": tid})
        return changed, parsed.result

    def _neutralize_if_stopped(self, run_id: int, parsed) -> None:
        """Stopping a session interrupts it, and the runtime reports that as an error (Claude Code's
        final result is `error_during_execution`, the process exits non-zero). When control asked the
        run to stop, that is the stop working: show it as a neutral item that says why, keep the raw
        text in the item's data, and don't let it become the run's result."""
        if not any(i.kind == "error" and i.data.get("session_end") for i in parsed.items):
            return
        run = self.db.q1("SELECT stop_requested_at, stop_reason FROM runs WHERE id=?", (run_id,))
        if not run or not run["stop_requested_at"]:
            return
        sentence = stopped_sentence(run["stop_reason"])
        for i, item in enumerate(parsed.items):
            if item.kind == "error" and item.data.get("session_end"):
                kept = {k: v for k, v in item.data.items() if k != "session_end"} | {"was": item.text, "stopped": True}
                parsed.items[i] = Item("control", sentence if item.data["session_end"] != "exit" else f"The session ended after the stop ({item.data.get('returncode')})", True, kept)
        if parsed.result and not parsed.result.get("ok"):
            parsed.result["text"] = sentence

    def _count(self, run_id: int, sess: Session, parsed) -> bool:
        """Put a session's usage in the ledger and refresh the run's `used`."""
        run = self.db.q1("SELECT provider, bucket, used FROM runs WHERE id=?", (run_id,))
        assert run
        pid, bucket = run["provider"] or SUBSCRIPTION, run["bucket"] or "allowance"
        if parsed.usage:
            mid, model, usage = parsed.usage
            self.budgets.record_message(run_id, sess.id, pid, "assistant", bucket, mid, model, usage)
        if parsed.result and parsed.result.get("cost_usd") is not None:
            cost: float | None = float(parsed.result["cost_usd"])
            if sess.runtime == "claude-code":  # a running total for the conversation, which every step resumes
                row = self.db.q1("SELECT session_id FROM run_sessions WHERE id=?", (sess.id,))
                conv = parsed.session_id or (row["session_id"] if row else None)
                resumed = bool(self.db.q1("SELECT 1 FROM run_sessions WHERE session_id=? AND id<?", (conv, sess.id))) if conv else False
                cost = self.budgets.turn_cost(conv, cost, resumed=resumed)
            if cost is not None:  # else the messages' estimate stands
                self.budgets.record_total(run_id, sess.id, pid, "assistant", bucket, cost)
        used = self.budgets.run_pts(run_id)
        self.db.ex("UPDATE runs SET used=? WHERE id=?", (used, run_id))
        return abs(used - (run["used"] or 0)) >= 0.05

    # ── starting ───────────────────────────────────────────────────────────
    async def gate(self, provider: str, bucket: str = "allowance", starting: bool = True) -> tuple[bool, str | None, s.UsageReading | None]:
        """May a run on this provider start (or keep going)? Subscription: the usage gate (the
        sweep uses its ceiling instead of the weekly reserve). API: a key is saved."""
        if provider != SUBSCRIPTION:
            if not self.budgets.credential(provider):
                return False, "No API key is saved for this provider.", None
            cap = self.budgets.cap_reason(provider) if starting and bucket != "reply" else None
            return cap is None, cap, None
        reading = await self.usage.read(max_age=60, refresh=self._refresh_login if starting else None)
        if bucket == "sweep":
            ceiling = self.settings.get("SWEEP_CEILING_PCT")
            if reading.seven_day_pct is not None and reading.seven_day_pct >= ceiling:
                return False, f"Weekly usage is at {reading.seven_day_pct:.0f}%, the sweep ceiling ({ceiling:.0f}%).", reading
            g = self.usage.gate(reading, starting=False)
        else:
            g = self.usage.gate(reading, starting=starting)
        return g.open, g.reason, reading

    async def start(self, req: s.RunStart, kind: str = "wakeup", why: str = "manual", thread_id: int | None = None, *, provider: str | None = None,
                    bucket: str = "allowance", wakeup_id: int | None = None, routine_id: int | None = None, label: str | None = None) -> s.Run:
        async with self._start_lock:
            if self.active_run_id() is not None:
                raise Conflict("busy", "A run is already in progress.")
            from ..updates import draining
            if draining(self.db):
                raise Conflict("updating", "An update is waiting for the assistant to be idle, so no new runs start until it's done.")
            from ..pause import active as paused
            if paused(self.db, self.budgets.now()):
                raise Conflict("paused", "Everything is paused. Resume in Settings → System to start runs again.")
            reply = kind == "reply"
            provider = provider or self.budgets.role("replier" if reply else "worker").provider
            if provider not in self.budgets.provider_ids():
                raise Conflict("no_provider", f"The provider {provider} isn't set up or is turned off.")
            ok, reason, reading = await self.gate(provider, bucket)
            if not ok:
                raise Conflict("gate_closed", reason or "Runs can't start right now.")
            budget = req.budget
            if reply:
                bucket = "reply"
                left = self.budgets.reply_left(provider)
                if left <= 0:
                    raise Conflict("reply_budget_empty", "The reply budget for this week is used up; your message waits for the next wakeup.")
                budget = min(self.settings.get("REPLY_RUN_MAX_PTS"), left)
            elif budget is None:
                budget = self.settings.get("MANUAL_RUN_PTS")
            now = utcnow()
            minutes = self.settings.get("REPLY_MAX_MINUTES" if reply else "MANUAL_RUN_MAX_MINUTES" if why == "manual" else "WAKEUP_MAX_MINUTES")
            deadline = iso(now + timedelta(minutes=minutes))
            tier = req.tier or self.budgets.role("replier" if reply else "worker").tier
            detail = None if req.reason == "manual" else req.reason
            level = self.budgets.effective(provider).level
            mode = self.budgets.reviewer_config().mode if kind in ("wakeup", "routine") else "off"
            run_id = self.db.ex(
                "INSERT INTO runs(kind, why, why_detail, provider, tier, budget, status, started_at, deadline, usage_start_pct, thread_id, bucket, wakeup_id, routine_id, "
                "level, reviewer_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, why, detail, provider, tier, budget, "starting", iso(now), deadline, reading and reading.seven_day_pct, thread_id, bucket, wakeup_id, routine_id, level, mode),
            )
            label = label or (f"Reply now · {detail}" if reply else "Started by you")
            self.timeline(run_id, Item("control", f"{label} · {tier} · {budget:g} pts ({bucket})", True, {"usage": reading and reading.model_dump(mode="json")}))
            self._run_changed(run_id)
            self._drive_task(run_id)
            self.fire("run_started", {"run_id": run_id, "kind": kind, "why": why, "thread_id": thread_id, "provider": provider})
            return self.run(run_id)

    async def _refresh_login(self) -> None:
        if self.active_run_id() is not None:
            raise RunnerError("a run is in progress")
        await self.egress.acquire("login-refresh", 180, "refreshing the agent's Claude login")
        try:
            await self.runner.refresh_auth()
        finally:
            await self.egress.release("login-refresh", "login refreshed")

    WHY = {
        "manual": "the owner started this run (Wake now)",
        "owner_message": "the owner pressed Reply now",
        "planned": "your planned wakeup: {detail}",
        "fallback": "a fallback wakeup: nothing was planned for the coming nights. Plan your wakeups before you finish",
        "paced": ("a paced wakeup (the owner's Max level keeps you working through the week). {detail}. Spend it on "
                  "the most valuable work (owner requests, then follow-ups and claims, then the backlog, projects and new ones)"),
        "sweep": "a sweep wakeup: {detail}. Spend it on the most valuable backlog work, or end with IDLE if nothing is worth it",
        "routine": "a routine: {detail}",
        "owner": "a wakeup the owner added: {detail}",
        "promoted": "a Reply-now run the owner promoted to a full run",
    }

    def _system(self) -> str:
        stopping = "stopping_use_it.md" if self.settings.get("USE_WHOLE_BUDGET") else "stopping_planned.md"
        return _strip_comment((PROMPTS / "core.md").read_text()) + "\n\n" + _strip_comment((PROMPTS / stopping).read_text()).strip() + self.ext_prompt("system")

    def _prompt(self, run_id: int) -> tuple[str, str]:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        system = self._system()
        detail = run["why_detail"] or ""
        why = self.WHY.get(run["why"], run["why"]).format(detail=detail)
        unit = self.budgets.period_unit(run["provider"] or SUBSCRIPTION)
        bucket = {"allowance": f"the {unit}ly allowance", "reply": "the reply budget", "sweep": f"what the owner left unused this {unit}"}.get(run["bucket"], run["bucket"])
        deadline = f"{self.budgets.local(parse(run['deadline'])):%a %b %d %H:%M}" if run["deadline"] else "none"
        reviewer = run["reviewer_mode"] if run["reviewer_mode"] != "off" else "off"
        if reviewer != "off":
            reviewer += f" (`assistant reviewer ask|start|done`; it may use up to {self.budgets.reviewer_config().budget_share:.0%} of your budget)"
        brief = _strip_comment((PROMPTS / "brief.md").read_text()).format(
            run_id=run_id, why=why, budget=f"{run['budget'] or 0:g}", bucket=bucket, deadline=deadline, tier=run["tier"], level=(run["level"] or "?").capitalize(),
            reviewer=reviewer)
        if run["wakeup_id"]:
            w = self.db.q1("SELECT note FROM wakeups WHERE id=?", (run["wakeup_id"],))
            if w and w["note"]:
                brief += "\n\n## Your note for this wakeup\n\nYou wrote this when you planned it. It's your own text, from an earlier run.\n\n" + w["note"]
        instructions = self.budgets.instructions().text
        if instructions:
            brief += "\n\n## Standing instructions from the owner\n\n" + instructions
        since = self._since_last_run(run_id)
        if since:
            brief += "\n\n## Since your last run\n\n" + since
        if run["routine_id"]:
            r = self.db.q1("SELECT name, prompt FROM routines WHERE id=?", (run["routine_id"],))
            if r:
                brief += f"\n\n## Routine: {r['name']}\n\n" + _strip_comment(r["prompt"])
        if run["thread_id"]:
            t = self.conv.thread(run["thread_id"])
            promote_min = self.settings.get("PROMOTE_WAIT_MINUTES")
            reply = _strip_comment((PROMPTS / "reply.md").read_text()).format(
                thread_id=t.id, title=t.title, project=t.project or "General", budget=f"{run['budget'] or 0:g}",
                promote_wait=f"{min(promote_min, 9.5):g}", promote_expiry=f"{promote_min:g}")
            brief += reply + "\n\n" + self.conv.thread_history(t.id)
        inbox, counts = self.conv.deliver(run_id=run_id)
        brief += "\n\n## Inbox\n\n" + (inbox or "Nothing new.")
        if counts:
            self.timeline(run_id, Item("control", "Delivered in the brief: " + _counts_text(counts), True, {"counts": counts}))
        cap = int(self.settings.get("BRIEF_LIST_CAP"))
        claims = self.conv.claims(active=True)
        if claims:
            brief += "\n\n## Claims still open from earlier runs\n\n" + "\n".join(f"- thread #{c.thread_id} “{c.thread_title}”: {c.plan}" for c in claims[:cap])
            if len(claims) > cap:
                brief += f"\n- {len(claims) - cap} more open claims: close the finished ones with `assistant claim done N \"result\"` and they'll show here"
        waiting = self.conv.inbox_entries(True, [])
        if waiting:
            brief += "\n\n## Waiting on the owner\n\n" + "\n".join(self._waiting_line(e) for e in waiting[:cap])
            if len(waiting) > cap:
                brief += f"\n- {len(waiting) - cap} more: `assistant asks --status open`"
        if run["kind"] != "reply":
            brief += "\n\n## Budget and your wakeup plan\n\n" + self.budgets.check_text(run["provider"] or SUBSCRIPTION)
            if self.brief_extra:
                extra = self.brief_extra(run)
                if extra:
                    brief += "\n\n" + extra
        if run["kind"] != "reply":
            brief += self.ext_prompt("wakeup")
        news = self.db.q("SELECT id, date, title, body FROM harness_news WHERE shown_in_run_id IS NULL ORDER BY date, id")
        if news:
            shown = news[-int(self.settings.get("NEWS_CAP")):]  # the newest; all count as shown, and `assistant news` has the rest
            brief += "\n\n## Harness news (shown once; `assistant news` lists all)\n\n"
            if len(news) > len(shown):
                brief += f"{len(news) - len(shown)} older entries not shown here: `assistant news`\n\n"
            brief += "\n\n".join(f"### {n['date']}: {n['title']}\n\n{n['body']}" for n in shown)
            self.db.ex(f"UPDATE harness_news SET shown_in_run_id=? WHERE id IN ({','.join('?' * len(news))})", (run_id, *[n["id"] for n in news]))
        self.timeline(run_id, Item("control", f"Brief: {len(brief):,} characters (system prompt {len(system):,})", False, {"brief_chars": len(brief), "system_chars": len(system)}))
        return brief, system

    def _waiting_line(self, e: s.InboxEntry) -> str:
        obj = getattr(e, e.kind, None)
        text = getattr(obj, "reason", None) if e.kind == "promote" else getattr(obj, "text", None)
        where = f" in thread #{e.thread_id} “{e.thread_title}”" if e.thread_id else ""
        body = " ".join((text or "").split())
        body = body[:100] + ("…" if len(body) > 100 else "")
        return f"- {e.kind} #{e.id}{where}: {body}, asked {_ago(e.at)}"

    def _since_last_run(self, run_id: int) -> str:
        """What control knows about the previous run, for the brief: how it ended and why, its last
        status sentence and notes, claims it left open and files only the safety-net commit saved."""
        prev = self.db.q1("SELECT * FROM runs WHERE id<? AND status NOT IN ('starting','running') AND kind!='reviewer' ORDER BY id DESC LIMIT 1", (run_id,))
        if not prev:
            return ""
        pid = prev["id"]
        when = self.budgets.local(parse(prev["ended_at"] or prev["started_at"]))
        used = f"used {prev['used'] or 0:.1f} of {prev['budget'] or 0:g} {UNIT}"
        if prev["status"] == "stopped":
            why = (ending("stopped", prev["end_reason"], prev["stop_reason"]) or "").rstrip(".").removeprefix("Stopped: ").removeprefix("Stopped ")
            how = f"was stopped ({why}), {used}"
        elif prev["status"] == "failed":
            how = f"failed ({(ending('failed', prev['end_reason'], prev['stop_reason'], prev['result']) or '').rstrip('.')}), {used}"
        elif prev["idle"]:
            how = f"ended on its own with IDLE, {used}"
        else:
            how = f"ended without writing IDLE, {used}"
        lines = [f"- Run #{pid} ({prev['kind']}, {prev['why']}) ended {when:%a %b %d %H:%M}: it {how}."]
        if prev["now_sentence"]:
            lines.append(f"- Its last status: {prev['now_sentence']}")
        notes = self.db.q("SELECT text FROM run_notes WHERE run_id=? ORDER BY id DESC LIMIT 3", (pid,))
        if notes:
            lines.append("- Its last notes: " + " | ".join(n["text"] for n in reversed(notes)))
        last_word = self.db.q1("SELECT body FROM reviewer_messages WHERE run_id=? AND sender='reviewer' AND kind='reply' ORDER BY id DESC LIMIT 1", (pid,))
        if last_word and last_word["body"]:
            # What it said as the run ended never reached the agent when it agreed (run 38, 2026-11-25: "do X, then end. AGREE").
            word = " ".join(last_word["body"].split())
            lines.append("- The reviewer's last word: " + (word if len(word) <= 900 else word[:897] + "…"))
        left = self.db.q("SELECT thread_id, plan FROM claims WHERE run_id=? AND status='partial' AND result=?", (pid, CLAIM_UNCLOSED))
        if left:
            lines.append("- Claims it left open: " + "; ".join(f"thread #{c['thread_id']} “{c['plan'][:60]}”" for c in left[:5]))
        for item in self.db.q("SELECT data_json FROM timeline_items WHERE run_id=? AND kind='diff' ORDER BY id", (pid,)):
            try:
                data = json.loads(item["data_json"])
            except ValueError:
                continue
            files = [f["path"] for f in data.get("files", []) if f.get("path")]
            if data.get("volume") == "workspace" and files:
                more = f" (+{len(files) - 10} more)" if len(files) > 10 else ""
                lines.append("- Files it changed without committing (control's safety-net commit saved them): " + ", ".join(files[:10]) + more)
        return "\n".join(lines)

    def _mcp_servers(self) -> list[dict]:
        rows = self.db.q(
            "SELECT i.name, i.title FROM integrations i WHERE i.enabled=1 AND EXISTS "
            "(SELECT 1 FROM integration_tools t WHERE t.integration_id=i.id AND t.present=1 AND t.policy!='blocked') ORDER BY i.name")
        return [{"name": r["name"], "title": r["title"]} for r in rows]

    async def _start_session(self, run_id: int, resume: str | None = None, reason: str | None = None, prompt: str | None = None) -> Session:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if run and run["switch_tier"] and resume:
            # A tier change asked for while no session was running (e.g. in the reviewer's reply at the gate) takes effect
            # on this resume, not one session late (run 20, 2026-11-21, resumed on sonnet after the reviewer chose opus).
            self.db.ex("UPDATE runs SET tier=?, switch_tier=NULL, tier_changes=tier_changes+1 WHERE id=?", (run["switch_tier"], run_id))
            self.timeline(run_id, Item("model", f"Resuming on {run['switch_tier']}", True, {"tier": run["switch_tier"]}))
            run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        pid = run["provider"] or SUBSCRIPTION  # type: ignore[index]
        spec = self.budgets.runtime_spec(pid, run["tier"])  # type: ignore[index]
        adapter = self.adapter(spec["runtime"])
        if prompt is not None:
            system = self._system()
        elif resume:
            prompt, system = adapter.interrupted_prompt(reason or "restart"), self._system()
        else:
            prompt, system = self._prompt(run_id)
        env = {**self.vault.env(run_id), **self.budgets.provider_env(pid)}
        payload = adapter.session_request(run_id=run_id, prompt=prompt, system=system, tier=run["tier"], resume_session=resume, env=env,  # type: ignore[index]
                                          model=spec["model"], effort=spec["effort"], mcp=self._mcp_servers())
        sid = await self.runner.start(payload)
        row_id = self.db.ex(
            "INSERT INTO run_sessions(run_id, runtime, session_id, model, effort, log_path, runner_session, started_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, adapter.name, resume or "", payload.get("model"), payload.get("effort"), f"runner:{sid}", sid, self.db.now()),
        )
        return Session(row_id, sid, 0, resume or "", adapter.name)

    def _stop_requested(self, run_id: int) -> bool:
        row = self.db.q1("SELECT stop_requested_at FROM runs WHERE id=?", (run_id,))
        return bool(row and row["stop_requested_at"])

    async def _wait_until_set_up(self, run_id: int) -> None:
        """While the agent computer is still setting itself up (after an update it replays the agent's provision.sh),
        wait for it with a note on the timeline instead of failing. Raises RunnerSettingUp when the run is stopped
        meanwhile or after SETUP_WAIT_S; any other runner error is left for the session start to report."""
        loop = asyncio.get_running_loop()
        deadline, noted, handover = loop.time() + SETUP_WAIT_S, False, None
        while True:
            try:
                await self.runner.health()
                return
            except RunnerSettingUp as e:
                if not noted:
                    self.timeline(run_id, Item("control", f"{e} This run starts when it's ready.", True))
                    noted = True
                if loop.time() >= deadline or self._stop_requested(run_id):
                    raise
                await asyncio.sleep(SETUP_POLL_S)
            except RunnerError:
                # Setup just ended: the stand-in has stopped and the runner is starting on its port (a second or two).
                if not noted:
                    return
                handover = handover or loop.time() + HANDOVER_S
                if loop.time() >= handover:
                    return
                await asyncio.sleep(1)

    # ── driving one run ────────────────────────────────────────────────────
    async def _drive(self, run_id: int) -> None:
        monitor = asyncio.create_task(self._monitor(run_id))
        try:
            await self.egress.acquire(f"run-{run_id}", LEASE_TTL, f"run #{run_id}")
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't open the agent's access to the model provider ({e}). "
                                              "The session will likely fail; check that the egress container is running.", True))
        last_result: dict | None = None
        end_state = "exited"
        try:
            while True:
                row = self.db.q1("SELECT * FROM run_sessions WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
                if row is None:
                    try:
                        await self._wait_until_set_up(run_id)
                        sess = await self._start_session(run_id)
                    except RunnerSettingUp as e:
                        if not self._stop_requested(run_id):
                            self.timeline(run_id, Item("error", f"{e} Gave up waiting after {SETUP_WAIT_S // 60} minutes; "
                                                                "see /workspace/system/provision.log.", True))
                        end_state = "failed_to_start"
                        break
                    except RunnerError as e:
                        self.timeline(run_id, Item("error", f"The agent computer refused to start the session: {e}. Check that the agent container is running.", True))
                        end_state = "failed_to_start"
                        break
                elif row["state"] == "running":
                    sess = Session(row["id"], row["runner_session"], row["log_offset"], row["session_id"], row["runtime"] or "claude-code")
                else:  # re-attached after the last session ended (e.g. while holding for a reply)
                    sess = Session(row["id"], row["runner_session"], row["log_offset"], row["session_id"], row["runtime"] or "claude-code")
                    end_state = row["state"]
                    ok = bool(row["result_ok"])
                    last_result = last_result or {"ok": ok, "text": (self.db.q1("SELECT result FROM runs WHERE id=?", (run_id,)) or {}).get("result") or ""}
                    if end_state == "exited" and ok and await self._hold(run_id, sess):
                        continue
                    break
                state, result = await self._follow(run_id, sess)
                last_result = result or last_result
                ok = bool(result and result.get("ok"))
                self.db.ex("UPDATE run_sessions SET state=?, ended_at=?, result_ok=COALESCE(result_ok, ?) WHERE id=?", (state, self.db.now(), int(ok), sess.id))
                end_state = state
                if state in ("exited", "lost") and await self._apply_switch(run_id, sess):
                    continue
                if state == "lost" and await self._should_resume(run_id, sess):
                    continue
                gate = await self._gate(run_id, sess, result) if state == "exited" and ok else "off"
                if gate == "resumed":
                    continue
                if state == "exited" and ok and await self._hold(run_id, sess):
                    continue
                if state == "exited" and ok and gate in ("off", "silent") and await self._nudge(run_id, sess, result):
                    continue
                break
            await self._finish(run_id, end_state, last_result)
        except asyncio.CancelledError:
            raise  # shutdown: the run carries on in the runner; the next start re-attaches
        except Exception as e:  # noqa: BLE001
            log.exception("run %s failed", run_id)
            self.timeline(run_id, Item("error", f"Something went wrong inside control: {explain_error(str(e))} The run was ended; control's log has the details.", True, {"raw": str(e)}))
            await self._finish(run_id, "error", last_result)
        finally:
            monitor.cancel()

    async def _follow(self, run_id: int, sess: Session) -> tuple[str, dict | None]:
        """Follow a session's log to its end. Returns (exited | lost, the result event)."""
        result: dict | None = None
        backoff, out_of_touch_since = 1.0, None
        while True:
            try:
                async for event, size in self.runner.log(sess.runner_session, sess.offset):
                    if out_of_touch_since is not None:  # the stream opened, or a heartbeat or event arrived
                        self.timeline(run_id, Item("control", "Back in touch with the agent computer", True))
                    out_of_touch_since, backoff = None, 1.0
                    if event is None:  # the stream opened, or a heartbeat: contact, nothing to store
                        continue
                    changed, r = self._process(run_id, sess, event, size)
                    result = r or result
                    if changed:
                        self._run_changed(run_id)
                st = await self.runner.session(sess.runner_session)
                if st["state"] != "running" and sess.offset >= st["size"]:
                    return ("lost" if st["state"] == "lost" else "exited"), result
                await asyncio.sleep(0.5)
            except RunnerIdle:
                continue  # a quiet stream, reconnected right away: not lost contact (issue #1)
            except RunnerError as e:
                if e.status == 404:
                    self.timeline(run_id, Item("error", "The agent computer no longer has this session's log (it was probably reset). "
                                              "The run ends here; what the agent saved to memory is kept.", True))
                    return "lost", result
                now = utcnow()
                give_up_min = self.settings.get("LOST_CONTACT_GIVE_UP_MINUTES")
                if out_of_touch_since is None:
                    out_of_touch_since = now
                    self.timeline(run_id, Item("control", "Lost touch with the agent computer; retrying", True, {"error": str(e)}))
                elif (now - out_of_touch_since).total_seconds() > give_up_min * 60:
                    self.timeline(run_id, Item("error", f"Gave up after {give_up_min:g} minutes without the agent computer. "
                                              "Check that the agent container is running; the next run starts fresh.", True))
                    return "unreachable", result
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)

    async def _should_resume(self, run_id: int, sess: Session) -> bool:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        if run["stop_requested_at"]:
            return False
        if not self.adapter(sess.runtime).capabilities.resume or not sess.runtime_session:
            self.timeline(run_id, Item("control", "Not resumed: the session never reported an id to resume", True))
            return False
        if run["resumes"] >= self.settings.get("RUN_MAX_RESUMES"):
            self.timeline(run_id, Item("control", f"Not resumed: already resumed {run['resumes']} times", True))
            return False
        if run["deadline"] and parse(run["deadline"]) <= utcnow():  # type: ignore[operator]
            return False
        for _ in range(60):  # wait up to ~5 minutes for the runner to be back
            try:
                if not (await self.runner.health()).get("busy"):
                    break
            except RunnerError:
                pass
            await asyncio.sleep(5)
        else:
            self.timeline(run_id, Item("error", "Not resumed: the agent computer didn't come back. Check that the agent container is running.", True))
            return False
        try:
            await self.egress.acquire(f"run-{run_id}", LEASE_TTL, f"run #{run_id}")
            await self._start_session(run_id, resume=sess.runtime_session, reason="the agent computer restarted")
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Resuming failed: {e}", True))
            return False
        self.db.ex("UPDATE runs SET resumes=resumes+1 WHERE id=?", (run_id,))
        self.timeline(run_id, Item("control", "Resumed the conversation after the agent computer restarted", True))
        return True

    async def switch_tier(self, run_id: int, tier: str, by: str) -> None:
        """Move a running wakeup to another tier: interrupt the current step and resume the
        same conversation on the new tier (the agent is told to check the cut-off step)."""
        self.db.ex("UPDATE runs SET switch_tier=? WHERE id=?", (tier, run_id))
        self.timeline(run_id, Item("model", f"{by[0].upper() + by[1:]} changed the tier to {tier}; interrupting the current step", True, {"tier": tier, "by": by}))
        await self._signal(run_id, force=False)

    async def _apply_switch(self, run_id: int, sess: Session) -> bool:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        if not run["switch_tier"] or run["stop_requested_at"] or not sess.runtime_session:
            if run["switch_tier"]:
                self.db.ex("UPDATE runs SET switch_tier=NULL WHERE id=?", (run_id,))
            return False
        tier = run["switch_tier"]
        self.db.ex("UPDATE runs SET tier=?, switch_tier=NULL, tier_changes=tier_changes+1 WHERE id=?", (tier, run_id))
        try:
            await self._start_session(run_id, resume=sess.runtime_session,
                                      prompt=f"[control] The reviewer moved you to {tier} for the rest of this wakeup. The step you were in may have been cut off: check it, then carry on.")
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't resume on {tier}: {e}", True))
            return False
        self.timeline(run_id, Item("model", f"Resumed on {tier}", True, {"tier": tier}))
        self._run_changed(run_id)
        return True

    async def _gate(self, run_id: int, sess: Session, result: dict | None) -> str:
        """Gate mode: the reviewer must agree before a session that ends cleanly ends the run
        (whether or not it wrote `IDLE:`). If it doesn't, resume the conversation with its reply
        (at most REVIEWER_GATE_MAX_ROUNDS times). Returns "off" (no gate here), "agreed", "resumed",
        "done" (the gate is spent or it's too late) or "silent" (the reviewer gave no verdict: that is
        not an agreement, so control's own carry-on rule, `_nudge`, decides instead)."""
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        text = ((result or {}).get("text") or "").strip()
        if run["reviewer_mode"] != "gate" or not self.reviewer or run["stop_requested_at"] or not sess.runtime_session:
            return "off"
        if run["gate_rounds"] >= self.settings.get("REVIEWER_GATE_MAX_ROUNDS"):
            self.timeline(run_id, Item("control", f"The reviewer gate already sent it back {run['gate_rounds']} times; letting the run end", True))
            return "done"
        if run["deadline"] and parse(run["deadline"]) <= utcnow() + timedelta(minutes=3):  # type: ignore[operator]
            return "done"
        left = (run["budget"] or 0) - (run["used"] or 0)
        idle_note = "" if ends_idle(text) else " (it didn't write IDLE)"
        agree, reply = await self.reviewer.approve(run_id, f"The assistant wants to end the wakeup{idle_note}. Budget left: {left:.1f} pts. Its final message:", text or "(none)")
        if agree is None:
            self.timeline(run_id, Item("control", "The reviewer gave no verdict on ending the wakeup; control's own carry-on rule decides", True))
            return "silent"
        if agree:
            self.timeline(run_id, Item("reviewer", "The reviewer agreed to end the wakeup", True))
            return "agreed"
        self.db.ex("UPDATE runs SET gate_rounds=gate_rounds+1 WHERE id=?", (run_id,))
        try:
            await self._start_session(run_id, resume=sess.runtime_session, prompt=f"[control] The reviewer doesn't agree that you should stop yet:\n\n{reply}")
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't resume after the reviewer's reply: {e}", True))
            return "done"
        self.timeline(run_id, Item("control", "Resumed: the reviewer didn't agree to end yet", True))
        return "resumed"

    def _reached_out(self, run_id: int, sess: Session, run: dict) -> bool:
        started = (self.db.q1("SELECT started_at FROM run_sessions WHERE id=?", (sess.id,)) or {}).get("started_at", run["started_at"])
        return bool(self.db.q1("SELECT 1 FROM messages WHERE run_id=? AND sender='assistant' AND created_at>=? LIMIT 1", (run_id, started)) or any(
            self.db.q1(f"SELECT 1 FROM {t} WHERE run_id=? AND status='open' LIMIT 1", (run_id,)) for t in ("questions", "permissions", "tasks")))

    async def _nudge(self, run_id: int, sess: Session, result: dict | None) -> bool:
        """Outside gate mode (or when the gate's reviewer gave no verdict): a wakeup that stopped on its own without `IDLE:` while more than
        NUDGE_MIN_LEFT_PCT of its budget is left is told to carry on, up to NUDGE_MAX times. With
        USE_WHOLE_BUDGET on, an `IDLE:` with budget left is nudged too (the owner's use-it-or-lose-it)."""
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        if run["kind"] == "reply" or run["bucket"] == "reply" or run["stop_requested_at"] or not sess.runtime_session or not run["budget"]:
            return False
        use_it = bool(self.settings.get("USE_WHOLE_BUDGET")) or run["bucket"] == "sweep"
        text = (result or {}).get("text")
        if (ends_idle(text) and not use_it) or self._reached_out(run_id, sess, run):
            return False
        done = self.db.q1("SELECT COUNT(*) AS n FROM timeline_items WHERE run_id=? AND kind='control' AND text LIKE ?", (run_id, NUDGE_MARK + "%"))["n"]
        if done >= self.settings.get("NUDGE_MAX"):
            return False
        used = self.budgets.run_pts(run_id)
        left = run["budget"] - used
        if left <= run["budget"] * self.settings.get("NUDGE_MIN_LEFT_PCT") / 100:
            return False
        if run["deadline"] and parse(run["deadline"]) <= utcnow() + timedelta(minutes=3):  # type: ignore[operator]
            return False
        minutes = (parse(run["deadline"]) - utcnow()).total_seconds() / 60 if run["deadline"] else None  # type: ignore[operator]
        time_left = f" and about {minutes:.0f} minutes" if minutes is not None else ""
        why = "You wrote IDLE" if ends_idle(text) else "You ended your turn"
        self.timeline(run_id, Item("control", f"{NUDGE_MARK} ({done + 1}): it stopped with {left:.0f} {UNIT} of its budget left", True))
        if use_it:
            prompt = (f"[control] {why}, but this wakeup still has {left:.0f} {UNIT}{time_left} left (carry-on {done + 1} of "
                      f"{self.settings.get('NUDGE_MAX')}). The owner runs on use it or lose it: what you leave isn't saved for anything. "
                      "Finishing the planned work isn't a reason to stop. Take the next worthwhile thing: the backlog's \"This week\", "
                      "the free-time queue, a project's next milestone, a tool, research, your own time. If the backlog is really empty, "
                      "write new substantial items first (each with a \"stop when\"), then start the first. No busywork. Leave an "
                      "`assistant note` about what you pick up, and keep going until control stops you. Only if everything worthwhile "
                      "is blocked on the owner, end with `IDLE: <reason>` saying so.")
        else:
            prompt = (f"[control] You ended your turn with {left:.0f} {UNIT}{time_left} of this wakeup's budget left. Carry on with the "
                      "next useful thing (the backlog, then the free-time queue), or end with `IDLE: <reason>` as the last line if "
                      "nothing worthwhile is left.")
        try:
            await self._start_session(run_id, resume=sess.runtime_session, prompt=prompt)
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't resume after the nudge: {e}", True))
            return False
        return True

    async def _hold(self, run_id: int, sess: Session) -> bool:
        """Hold a run open for the owner's reply if it reached out during the session (a
        message or an open ask). If the owner responds in time, resume the same
        conversation with what they said and return True."""
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        minutes = self.settings.get("HOLD_MINUTES")
        if run["stop_requested_at"] or minutes <= 0 or not sess.runtime_session or run["hold_rounds"] >= self.settings.get("HOLD_MAX_ROUNDS"):
            return False
        if not run["waiting_until"]:
            if run["deadline"] and parse(run["deadline"]) <= utcnow():  # type: ignore[operator]
                return False
            reached_out = self._reached_out(run_id, sess, run)
            if not reached_out:
                return False
            # Every wait gets the full window, and waiting doesn't use up the run's wall-clock time: the
            # deadline moves by the time spent waiting (owner, 2026-11-18: the window restarts on each reply).
            until = utcnow() + timedelta(minutes=minutes)
            self.db.ex("UPDATE runs SET waiting_until=?, hold_started_at=?, now_sentence=? WHERE id=?",
                       (iso(until), self.db.now(), "Waiting for your reply", run_id))
            self.timeline(run_id, Item("control", f"Waiting up to {minutes:g} min for your reply", True))
            self._run_changed(run_id)
        until = parse(self.db.q1("SELECT waiting_until FROM runs WHERE id=?", (run_id,))["waiting_until"])
        while utcnow() < until:  # type: ignore[operator]
            if self.db.q1("SELECT stop_requested_at FROM runs WHERE id=?", (run_id,))["stop_requested_at"]:
                return False
            if self.conv.deliver(peek=True, peers=False)[0]:  # a friend's message doesn't end the wait for the owner
                break
            await asyncio.sleep(1)
        else:
            self._end_hold(run_id)
            self.timeline(run_id, Item("control", "No reply in time; finishing", True))
            return False
        self._end_hold(run_id)
        text, counts = self.conv.deliver(peers=False)
        self.db.ex("UPDATE runs SET waiting_until=NULL, hold_rounds=hold_rounds+1, now_sentence=? WHERE id=?", ("Reading your reply", run_id))
        try:
            await self._start_session(run_id, resume=sess.runtime_session, prompt=f"[control] The owner responded while you were waiting:\n\n{text}")
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't resume for your reply: {e}", True))
            return False
        self.timeline(run_id, Item("control", "Resumed with your reply: " + _counts_text(counts), True, {"counts": counts}))
        self._run_changed(run_id)
        return True

    def _end_hold(self, run_id: int) -> None:
        """Clear the wait and move the deadline by the time spent waiting."""
        row = self.db.q1("SELECT deadline, hold_started_at FROM runs WHERE id=?", (run_id,))
        if row and row["deadline"] and row["hold_started_at"]:
            waited = utcnow() - parse(row["hold_started_at"])  # type: ignore[operator]
            self.db.ex("UPDATE runs SET deadline=? WHERE id=?", (iso(parse(row["deadline"]) + waited), run_id))  # type: ignore[operator]
        self.db.ex("UPDATE runs SET waiting_until=NULL, hold_started_at=NULL WHERE id=?", (run_id,))

    async def _finish(self, run_id: int, end_state: str, result: dict | None) -> None:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        assert run
        if run["status"] not in ACTIVE:
            return
        if run["stop_requested_at"]:
            status, reason = "stopped", run["stop_reason"] or "stopped"
        elif result and result.get("ok"):
            status, reason = "done", "idle" if ends_idle(result.get("text")) else "finished"
        else:
            status, reason = "failed", "error"
        if status == "stopped":
            r = (run["stop_reason"] or "").lower()
            reason = "budget" if "budget" in r else "deadline" if "deadline" in r else "usage" if "usage" in r or "reserve" in r or "ceiling" in r else "stopped"
        idle = bool(result and ends_idle(result.get("text")))
        try:
            commit = await self.runner.commit(f"run #{run_id} ({status})", run["workspace_head"])
            files = [f for f in commit.get("files", []) if f.get("path")]
            if commit.get("status") == "error":
                self.timeline(run_id, Item("error", f"Couldn't save the agent's memory folder to git ({commit.get('error')}). "
                                                    "The next run tries again.", True, {"raw": str(commit.get("error"))}))
            elif files:
                self.timeline(run_id, Item("diff", f"Memory: {len(files)} file(s) changed", True, {"volume": "workspace", "files": files, "commit": commit.get("commit")}))
        except RunnerSettingUp:
            self.timeline(run_id, Item("control", "Memory commit skipped: the agent computer is setting up. The next run's commit includes any changes.", False))
        except RunnerError as e:
            self.timeline(run_id, Item("error", f"Couldn't save the agent's memory folder to git ({e}). The next run tries again.", False, {"raw": str(e)}))
        used = self.budgets.run_pts(run_id)
        if (run["provider"] or SUBSCRIPTION) == SUBSCRIPTION and run["usage_start_pct"] is not None:
            try:
                end = await self.usage.read(max_age=30)
                if end.seven_day_pct is not None:
                    self.budgets.calibrate((end.seven_day_pct - run["usage_start_pct"]) * 10, self.budgets.run_cost(run_id))
            except Exception:  # noqa: BLE001
                pass
        self.conv.drop_open_claims(run_id)
        self.conv.expire_promotes(run_id)
        for undone in self.conv.revert_once():
            self.timeline(run_id, Item("control", f"Undid a one-time approval: {undone}", True))
        self.db.ex("UPDATE runs SET status=?, end_reason=?, ended_at=?, used=?, idle=?, waiting_until=NULL WHERE id=?", (status, reason, self.db.now(), used, int(idle), run_id))
        said = (ending("stopped", reason, run["stop_reason"]) or "Stopped.").rstrip(".")
        label = {"done": "Run finished", "stopped": "Run " + said[:1].lower() + said[1:], "failed": "Run failed"}[status] + f" · used {used:.1f} of {run['budget'] or 0:g} pts"
        if end_state in ("unreachable", "failed_to_start"):
            label += " · the agent computer was unavailable"
        self.timeline(run_id, Item("control", label, True))
        try:
            await self.egress.release(f"run-{run_id}", f"run #{run_id} ended")
        except RunnerError as e:
            log.warning("couldn't revoke the lease (it expires on its own): %s", e)
        self._run_changed(run_id)
        done = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        for hook in self.on_finish:
            try:
                hook(done)
            except Exception:  # noqa: BLE001
                log.exception("run-finish hook failed")

    def promote(self, run_id: int, extra: float) -> bool:
        """An approved promote request: the reply run carries on as a full run, on the main
        allowance, with more budget and a wakeup's deadline."""
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or run["status"] not in ACTIVE:
            return False
        deadline = max(parse(run["deadline"]) or utcnow(), utcnow() + timedelta(minutes=self.settings.get("WAKEUP_MAX_MINUTES")))  # type: ignore[type-var]
        self.db.ex("UPDATE runs SET bucket='allowance', why='promoted', budget=?, deadline=?, reviewer_mode=? WHERE id=?",
                   ((run["used"] or 0) + extra, iso(deadline), self.budgets.reviewer_config().mode, run_id))
        self.timeline(run_id, Item("control", f"Promoted to a full run: +{extra:g} pts from the allowance, deadline {iso(deadline)}", True))
        self._run_changed(run_id)
        return True

    # ── stopping and limits ────────────────────────────────────────────────
    async def stop(self, run_id: int, reason: str = "stopped by you") -> s.Run:
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise NotFound(f"run {run_id}")
        if run["status"] in ACTIVE and not run["stop_requested_at"]:
            self.db.ex("UPDATE runs SET stop_requested_at=?, stop_reason=? WHERE id=?", (self.db.now(), reason, run_id))
            self.timeline(run_id, Item("control", f"Stop requested: {reason}", True))
            await self._signal(run_id, force=False)
            self._run_changed(run_id)
        return self.run(run_id)

    def add_budget(self, run_id: int, add: float) -> s.Run:
        """The owner gives a running run more points. The monitor rereads the budget every few seconds."""
        run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise NotFound(f"run {run_id}")
        if run["status"] not in ACTIVE or run["stop_requested_at"]:
            raise Conflict("not_running", "This run is ending or over, so its budget can't change.")
        budget = round((run["budget"] or 0) + add, 2)
        self.db.ex("UPDATE runs SET budget=? WHERE id=?", (budget, run_id))
        self.timeline(run_id, Item("control", f"You added {add:g} pts · the budget is now {budget:g} pts", True))
        self._run_changed(run_id)
        return self.run(run_id)

    async def _signal(self, run_id: int, force: bool) -> None:
        sess = self.db.q1("SELECT runner_session FROM run_sessions WHERE run_id=? AND state='running' ORDER BY id DESC LIMIT 1", (run_id,))
        if not sess:
            return
        try:
            await self.runner.stop(sess["runner_session"], force=force)
        except RunnerError as e:
            log.warning("runner stop failed: %s", e)

    async def _monitor(self, run_id: int) -> None:
        last_usage = utcnow()
        escalated = revoked = False
        while True:
            await asyncio.sleep(MONITOR_S)
            run = self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
            if not run or run["status"] not in ACTIVE:
                return
            now = utcnow()
            if run["stop_requested_at"]:
                waited = (now - parse(run["stop_requested_at"])).total_seconds()  # type: ignore[operator]
                grace = self.settings.get("STOP_GRACE_SECONDS")
                if waited > grace and not escalated:
                    escalated = True
                    self.timeline(run_id, Item("control", "The session didn't stop in time; killing it", True))
                    await self._signal(run_id, force=True)
                if waited > grace + 30 and not revoked:
                    revoked = True
                    self.timeline(run_id, Item("control", "Revoked model-provider access for this run", True))
                    try:
                        await self.egress.release(f"run-{run_id}", f"run #{run_id} stopping")
                    except RunnerError:
                        pass
                continue
            if run["deadline"] and now >= parse(run["deadline"]) and not run["waiting_until"]:  # type: ignore[operator]
                await self.stop(run_id, "deadline reached")  # (time held open for the owner doesn't count: _hold moves the deadline)
                continue
            if run["budget"] is not None and (run["used"] or 0) >= run["budget"]:
                await self.stop(run_id, f"budget reached ({run['used']:.1f} of {run['budget']:g} pts)")
                continue
            if (now - last_usage).total_seconds() >= USAGE_CHECK_S and (run["provider"] or SUBSCRIPTION) == SUBSCRIPTION:
                last_usage = now
                try:
                    ok, reason, reading = await self.gate(SUBSCRIPTION, run["bucket"] or "allowance", starting=False)
                except Exception:  # noqa: BLE001
                    continue
                if reading is not None and reading.seven_day_pct is not None and not ok:
                    await self.stop(run_id, reason or "usage reserve reached")


def ends_idle(text: str | None) -> bool:
    """True when the first or the last non-empty line of a final message starts with `IDLE:`."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return bool(lines) and (lines[0].startswith("IDLE:") or lines[-1].startswith("IDLE:"))


def _ago(at) -> str:
    secs = max(0, (utcnow() - at).total_seconds())
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {name}{'s' if n != 1 else ''} ago"
    return "just now"


def _strip_comment(text: str) -> str:
    """Drop a leading <!-- control note --> from a prompt file."""
    text = text.lstrip()
    if text.startswith("<!--"):
        text = text[text.index("-->") + 3:]
    return text.strip("\n") + "\n" if text.strip() else ""


def _counts_text(counts: dict[str, int]) -> str:
    names = {"messages": "message", "answers": "answer", "decisions": "decision", "tasks": "finished task", "suggestions": "suggestion decision",
             "secrets": "new secret", "notes": "note to itself", "reviewer": "reviewer reply", "peer_messages": "friend message"}
    return ", ".join(f"{n} {names.get(k, k)}{'s' if n != 1 else ''}" for k, n in counts.items())
