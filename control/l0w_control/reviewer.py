"""The reviewer: a stronger model that watches wakeups and talks with the agent.

Modes (Settings → Reviewer; on by default in Monitor):
- **Advisor**: answers when the agent asks (`assistant reviewer ask "…"`).
- **Monitor**: also reads the run's notes, status, claims and asks every `interval_minutes`
  (only when something new happened) and may interject; it answers `QUIET` otherwise.
- **Gate**: also approves the end of the run (a session ending `IDLE:`) and finished claims
  (`assistant claim done`). If it doesn't agree, control resumes the session with its reply,
  at most REVIEWER_GATE_MAX_ROUNDS times.

Each turn runs in the agent container through the runner's `/consult`: a second `claude -p`,
read-only, with no hook, secrets or agent token. Its usage goes into the ledger as 'reviewer'
on the run's bucket, counts toward the run's budget, and stops at `budget_share` of it. With
`change_tier` on, a `LEVEL: model/effort` line moves the worker to another allowed tier: control
interrupts the session and resumes the same conversation on the new tier.

Continuity: one reviewer conversation per local day (default) or per wakeup.
Replies to watch turns reach the agent like any mid-run delivery (after its next tool call);
replies to its own `ask`/`start`/`done` are returned to the waiting CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from .auth import parse, utcnow
from .budgets import Budgets, price
from .config import Settings
from .db import Database
from .live import Live
from .runs.clients import RunnerClient, RunnerError
from .runs.runtime import Item

if TYPE_CHECKING:
    from .runs.manager import RunManager

log = logging.getLogger("l0w.reviewer")
PROMPTS = Path(__file__).parent / "prompts"
LOOP_S = 20
MODE_TEXT = {
    "advisor": "**Advisor.** You answer when the assistant asks you something. You don't get watch updates.",
    "monitor": "**Monitor.** You answer its questions and get watch updates of its notes, so you can interject when needed.",
    "gate": ("**Gate.** As Monitor, and you also approve the end of the wakeup and finished tasks: until you end a reply "
             "with `AGREE`, it keeps working."),
}
# The end-of-run rule, by the owner's USE_WHOLE_BUDGET setting (2026-11-18; on = the old assistant's mindset).
STOP_TEXT = {
    True: ("**the default is no.** The owner runs on use it or lose it: what the wakeup leaves unspent isn't saved for anything, "
           "and \"the planned work is done\" is not a reason to stop. Its own `CLAUDE.md` may say to stop when the plan is done; that "
           "doesn't apply. Look at its backlog, free-time queue, roadmap and projects, and name the next concrete task (what, where, "
           "what done looks like). If the backlog is genuinely exhausted, tell it to write new substantial backlog items and start the "
           "first. Never push it into busywork. Agree only when continuing really has no value: the budget is nearly spent, or "
           "everything worthwhile is blocked on the owner. To agree, end with `AGREE` on its own line."),
    False: ("agree when continuing really has no value: the budget is nearly spent, everything worthwhile is blocked on the owner, "
            "or more work would be busywork. Then end with `AGREE` on its own line. Otherwise name the next concrete task (what, "
            "where, what done looks like). Its budget is planned, not use-it-or-lose-it, except in a sweep wakeup, where unspent "
            "budget is lost."),
}
LEVEL_RE = re.compile(r"^\s*LEVEL:\s*([a-z]+/[a-z]+)\s*$", re.M | re.I)
AGREE_RE = re.compile(r"^\s*AGREE\s*$", re.M)


class Reviewer:
    def __init__(self, db: Database, settings: Settings, budgets: Budgets, runner: RunnerClient, live: Live, redact):
        self.db, self.settings, self.budgets, self.runner, self.live, self.redact = db, settings, budgets, runner, live, redact
        self.runs: RunManager | None = None
        self.ext_prompt = lambda slot: ""  # extensions' prompt fragments, by slot
        self._last_watch: dict[int, tuple[float, int]] = {}   # run → (time, last timeline id seen)
        self._lock = asyncio.Lock()

    # ── state ──────────────────────────────────────────────────────────────
    def _run(self, run_id: int) -> dict | None:
        return self.db.q1("SELECT * FROM runs WHERE id=?", (run_id,))

    def mode(self, run_id: int) -> str:
        run = self._run(run_id)
        return (run or {}).get("reviewer_mode") or "off"

    def _session_key(self, run: dict) -> str:
        cfg = self.budgets.reviewer_config()
        if cfg.continuity == "wakeup":
            return f"reviewer_session:run:{run['id']}"
        return f"reviewer_session:day:{self.budgets.local(utcnow()).date().isoformat()}"

    def _spent(self, run_id: int) -> float:
        return float(self.db.q1("SELECT COALESCE(SUM(pts),0) AS p FROM usage_ledger WHERE run_id=? AND who='reviewer'", (run_id,))["p"])

    def _spent_usd(self, run_id: int) -> float:
        return float(self.db.q1("SELECT COALESCE(SUM(cost_usd),0) AS c FROM usage_ledger WHERE run_id=? AND who='reviewer'", (run_id,))["c"])

    def _spent_run_pts(self, run: dict) -> float:
        """The reviewer's spend in the run's own points (its provider's rate), the unit of the run's budget."""
        return self._spent_usd(run["id"]) * self.budgets.pts_per_usd(run["provider"])

    def _out_of_budget(self, run: dict) -> bool:
        # Compared in dollars: the reviewer may run on another provider, whose points mean something else.
        share = self.budgets.reviewer_config().budget_share
        limit_usd = share * (run["budget"] or 0) / max(self.budgets.pts_per_usd(run["provider"]), 1e-9)
        return bool(run["budget"]) and self._spent_usd(run["id"]) >= limit_usd

    def _add(self, run_id: int, sender: str, kind: str, body: str, reply_to: int | None = None, delivered: bool = False) -> int:
        return self.db.ex("INSERT INTO reviewer_messages(run_id, sender, kind, body, reply_to, delivered_at, created_at) VALUES(?,?,?,?,?,?,?)",
                          (run_id, sender, kind, self.redact(body.strip())[:20000], reply_to, self.db.now() if delivered else None, self.db.now()))

    # ── one turn ───────────────────────────────────────────────────────────
    def _system(self) -> str:
        cfg = self.budgets.reviewer_config()
        if cfg.change_tier:
            tier_text = ("## Setting the assistant's tier\nYou may move the assistant to another tier for this wakeup when the work turns out harder or "
                         f"easier than its tier suits. Allowed: {', '.join(cfg.tier_choices)}; at most {cfg.max_tier_changes} changes per wakeup. "
                         "Put `LEVEL: model/effort` on its own line. Control interrupts its current step and resumes the same conversation on the new tier "
                         "within a minute (the step in flight may be cut off; it's told to check). A switch rereads the whole conversation, so don't flip back and forth.")
        else:
            tier_text = "You can't change the assistant's tier in this setup."
        from .runs.manager import _strip_comment

        worker, me = self.budgets.role("worker"), self.budgets.role("reviewer")
        wlabel, mlabel = self.budgets._row(worker.provider)["label"], self.budgets._row(me.provider)["label"]
        worker_text = (f"The assistant runs on {wlabel} (default tier {worker.tier}). A tier change keeps its conversation, so it can only "
                       "move between tiers of that same provider, never to another runtime.")
        strong = me.tier.split("/")[0] == "opus"
        cost_text = (f"each of your turns spends the owner's usage on {mlabel} ({me.tier}"
                     + (", a strong model)" if strong else ", a cheaper model, but still the owner's usage)"))
        return Template(_strip_comment((PROMPTS / "reviewer.md").read_text())).safe_substitute(
            mode=cfg.mode.capitalize(), mode_text=MODE_TEXT.get(cfg.mode, ""), tier_text=tier_text,
            worker_text=worker_text, cost_text=cost_text, stop_text=STOP_TEXT[bool(self.settings.get("USE_WHOLE_BUDGET"))],
            share=f"{cfg.budget_share:.0%}", timezone=str(self.budgets.tz())) + self._owner_text() + self.ext_prompt("reviewer")

    def _orientation(self, run: dict) -> str:
        """The first turn of a new conversation: read the workspace, then keep a ranked list of next tasks.
        Control adds what it knows (the owner's recent messages, open claims, asks waiting on the owner, the plan)."""
        from .runs.manager import _strip_comment

        cfg = self.budgets.reviewer_config()
        scope = "wakeup" if cfg.continuity == "wakeup" else "day"
        use_it = bool(self.settings.get("USE_WHOLE_BUDGET"))
        stop_rule = ("use the whole budget (use it or lose it): finishing the planned work isn't a reason to stop; the default answer to "
                     "an early end is no, with the next concrete task." if use_it else
                     "the budget is planned: stopping early with `IDLE:` is fine when nothing worthwhile is left.")
        return Template(_strip_comment((PROMPTS / "reviewer_orientation.md").read_text())).safe_substitute(
            scope=scope, today=f"{self.budgets.local(utcnow()):%a %Y-%m-%d}", stop_rule=stop_rule, context=self._context(run))

    def _context(self, run: dict) -> str:
        parts = []
        msgs = self.db.q("SELECT m.body, m.created_at, m.delivered_at, t.title FROM messages m JOIN threads t ON t.id=m.thread_id "
                         "WHERE m.sender='owner' ORDER BY m.id DESC LIMIT 12")
        if msgs:
            lines = []
            for m in reversed(msgs):
                body = " ".join((m["body"] or "").split())
                body = body if len(body) <= 600 else body[:597] + "…"
                seen = "" if m["delivered_at"] else " (the assistant hasn't seen it yet)"
                lines.append(f"- {m['created_at'][:16].replace('T', ' ')} in “{m['title']}”{seen}: {body}")
            parts.append("The owner's recent messages, oldest first:\n" + "\n".join(lines))
        conv = getattr(self.runs, "conv", None)
        if conv:
            try:
                claims = conv.claims(active=True)
                if claims:
                    parts.append("Open claims (work it said it would do):\n" + "\n".join(f"- thread #{c.thread_id} “{c.thread_title}”: {c.plan}" for c in claims[:15]))
                waiting = conv.inbox_entries(True, [])
                if waiting:
                    parts.append(f"Waiting on the owner: {len(waiting)} ask(s) ("
                                 + ", ".join(sorted({e.kind for e in waiting})) + "). Work that depends on them is blocked; the rest isn't.")
            except Exception:  # noqa: BLE001  (orientation must never fail a turn)
                pass
        planned = self.db.q("SELECT at, budget, reason FROM wakeups WHERE status='planned' ORDER BY at LIMIT 5")
        if planned:
            parts.append("Planned wakeups:\n" + "\n".join(f"- {w['at'][:16].replace('T', ' ')} UTC, {w['budget']:g} pts: {w['reason']}" for w in planned))
        parts.append(f"This run: #{run['id']}, {run['kind']} ({run['why']}{': ' + run['why_detail'] if run.get('why_detail') else ''}), "
                     f"budget {run['budget'] or 0:g} pts.")
        return "\n\n".join(parts)

    def _owner_news(self, key: str, first: bool) -> str:
        """The owner's messages since this conversation's last turn, in full (a new conversation's orientation
        already lists the recent ones). Watch updates used to say only "Delivered: 1 message", so the reviewer
        contradicted directions it never saw (run 40, 2026-11-25: the owner said not to wait, it said wait)."""
        seen_key = f"{key}:owner_seen"
        top = self.db.q1("SELECT COALESCE(MAX(id), 0) AS n FROM messages WHERE sender='owner'")["n"]
        row = self.db.q1("SELECT value FROM kv WHERE key=?", (seen_key,))
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (seen_key, json.dumps(top), self.db.now()))
        if first or row is None:
            return ""
        msgs = self.db.q("SELECT m.body, m.created_at, t.title FROM messages m JOIN threads t ON t.id=m.thread_id "
                         "WHERE m.sender='owner' AND m.id>? ORDER BY m.id LIMIT 20", (int(json.loads(row["value"])),))
        if not msgs:
            return ""
        lines = []
        for m in msgs:
            at = self.budgets.local(parse(m["created_at"]))  # type: ignore[arg-type]
            lines.append(f"- {at:%H:%M} in “{m['title']}”: {(m['body'] or '').strip()[:2000]}")
        return ("New from the owner since your last turn (they outrank you; never tell the assistant otherwise):\n"
                + "\n".join(lines) + "\n\n")

    def _owner_text(self) -> str:
        text = (self.budgets.instructions().reviewer_text or "").strip()
        return f"\n\n## Standing instructions from the owner, for you\n\nThe assistant doesn't see these.\n\n{text}" if text else ""

    def _header(self, run: dict) -> str:
        now = self.budgets.local(utcnow())
        return (f"[control] {now:%a %Y-%m-%d %H:%M} · run #{run['id']} ({run['kind']}, why: {run['why']}) · tier {run['tier']} · "
                f"budget {run['used'] or 0:.1f} of {run['budget'] or 0:g} pts used · your share so far {self._spent_run_pts(run):.1f} pts")

    async def turn(self, run_id: int, prompt: str, kind: str) -> tuple[str | None, int | None]:
        """Run one reviewer turn. Returns (reply or None when quiet/unavailable, the reply's message id).
        The end-of-run verdict ("end") runs even past the reviewer's share: it's the turn that matters most, and
        skipping it used to end runs with budget left without a word (runs 33 and 36, 2026-11-25)."""
        async with self._lock:
            run = self._run(run_id)
            if not run or run["status"] not in ("starting", "running") or (run["reviewer_mode"] or "off") == "off":
                return None, None
            if kind != "end" and self._out_of_budget(run):
                return None, None
            cfg = self.budgets.reviewer_config()
            pid = cfg.provider if cfg.provider in self.budgets.provider_ids() else run["provider"]
            spec = self.budgets.runtime_spec(pid, cfg.tier)
            key = self._session_key(run) + ("" if spec["runtime"] == "claude-code" else f":{spec['runtime']}")
            row = self.db.q1("SELECT value FROM kv WHERE key=?", (key,))
            resume = json.loads(row["value"]) if row else None
            first = f"{self._orientation(run)}\n\n" if resume is None else ""  # a new conversation orients itself first
            news = self._owner_news(key, resume is None)
            payload = {"run_id": run_id, "prompt": f"{first}{self._header(run)}\n\n{news}{prompt}", "system_append": self._system(), "runtime": spec["runtime"],
                       "model": spec["model"], "effort": spec["effort"], "resume_session": resume, "env": self.budgets.provider_env(pid)}
            try:
                out = await self.runner.consult(payload)
            except RunnerError as e:
                if resume:  # a stale conversation: start a new one once (and orient it)
                    payload["resume_session"] = None
                    payload["prompt"] = f"{self._orientation(run)}\n\n{self._header(run)}\n\n{prompt}"
                    try:
                        out = await self.runner.consult(payload)
                    except RunnerError as e2:
                        self._timeline(run_id, Item("error", f"The reviewer couldn't run: {e2}", True))
                        return None, None
                else:
                    self._timeline(run_id, Item("error", f"The reviewer couldn't run: {e}", True))
                    return None, None
            if out.get("session_id"):
                self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                           (key, json.dumps(out["session_id"]), self.db.now()))
            self._count(run, out, spec["runtime"], resumed=bool(payload.get("resume_session")))
            text = (out.get("text") or "").strip()
            if not out.get("ok") and not text:
                self._timeline(run_id, Item("error", "The reviewer's turn failed", True))
                return None, None
            quiet = text.upper().startswith("QUIET") and len(text) < 40
            mid = self._add(run_id, "reviewer", "quiet" if quiet else "reply", text or "QUIET")
            self._timeline(run_id, Item("reviewer", "Reviewer: quiet" if quiet else f"Reviewer: {text}", not quiet, {"message_id": mid, "turn": kind}))
            if not quiet:
                await self._maybe_switch(run_id, text)
            return (None if quiet else text), mid

    def _count(self, run: dict, out: dict, runtime: str = "claude-code", resumed: bool = False) -> None:
        """Add one turn's cost to the run's reviewer row in the ledger (the runtime's own
        total for the turn, else the messages priced at API rates). Claude Code's total is the
        whole day's conversation so far, so only what this turn added counts."""
        pid = self.budgets.reviewer_config().provider
        if pid not in self.budgets.provider_ids():
            pid = run["provider"]
        cost = out.get("cost_usd")
        if cost is not None and runtime == "claude-code":
            cost = self.budgets.turn_cost(out.get("session_id"), float(cost), resumed=resumed)
        if cost is None:
            cost = sum(price(m.get("model"), m.get("usage") or {}) for m in out.get("messages") or [])
        u = out.get("usage") or {}
        row = self.budgets._ledger_row(run["id"], None, pid, "reviewer", run["bucket"] or "allowance")
        self.db.ex("UPDATE usage_ledger SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, cache_read_tokens=cache_read_tokens+?, "
                   "cache_write_tokens=cache_write_tokens+?, cost_usd=cost_usd+?, pts=pts+?, cost_final=1, updated_at=? WHERE id=?",
                   (u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                    float(cost), float(cost) * self.budgets.pts_per_usd(pid), self.db.now(), row["id"]))
        used = self.budgets.run_pts(run["id"])
        self.db.ex("UPDATE runs SET used=?, reviewer_used=? WHERE id=?", (used, self._spent(run["id"]), run["id"]))
        self.live.changed("run", run["id"])

    async def _maybe_switch(self, run_id: int, text: str) -> None:
        m = LEVEL_RE.search(text)
        if not m or not self.runs:
            return
        cfg = self.budgets.reviewer_config()
        run = self._run(run_id)
        tier = m.group(1).lower()
        if not run or not cfg.change_tier:
            return
        if tier not in cfg.tier_choices:
            self._timeline(run_id, Item("control", f"Ignored the reviewer's tier change to {tier}: not one it may set", True))
            return
        if run["tier_changes"] >= cfg.max_tier_changes:
            self._timeline(run_id, Item("control", f"Ignored the reviewer's tier change to {tier}: already {run['tier_changes']} changes this wakeup", True))
            return
        if tier == run["tier"]:
            return
        await self.runs.switch_tier(run_id, tier, "the reviewer")

    def _timeline(self, run_id: int, item: Item) -> None:
        if self.runs:
            self.runs.timeline(run_id, item)

    # ── what the agent calls ───────────────────────────────────────────────
    async def from_agent(self, run_id: int, kind: str, body: str) -> str:
        run = self._run(run_id)
        mode = (run or {}).get("reviewer_mode") or "off"
        if mode == "off":
            return "There's no reviewer on this run."
        mid = self._add(run_id, "assistant", kind, body, delivered=True)
        self._timeline(run_id, Item("reviewer", f"To the reviewer ({kind}): {body}", True, {"message_id": mid}))
        lead = {"ask": "The assistant asks:", "start": "The assistant is starting a task:", "done": "The assistant says a task is done:"}[kind]
        reply, rid = await self.turn(run_id, f"{lead}\n\n{body}", kind)
        if rid:
            self.db.ex("UPDATE reviewer_messages SET delivered_at=?, reply_to=? WHERE id=?", (self.db.now(), mid, rid))
        if reply is None:
            return "(The reviewer has nothing to add.)" if run and not self._out_of_budget(run) else "(The reviewer is out of its share of this wakeup's budget.)"
        return reply

    async def approve(self, run_id: int, what: str, body: str) -> tuple[bool | None, str]:
        """Gate mode: does the reviewer agree (to the end of the run, or a finished task)? None when it gave no
        verdict (quiet, failed or out of its share): not an agreement; the caller falls back to its own rule."""
        reply, rid = await self.turn(run_id, f"{what}\n\n{body}", "end" if what.startswith("The assistant wants to end") else "done")
        if rid:
            self.db.ex("UPDATE reviewer_messages SET delivered_at=? WHERE id=?", (self.db.now(), rid))
        if reply is None:
            return None, ""
        return bool(AGREE_RE.search(reply)), reply

    # ── watching ───────────────────────────────────────────────────────────
    async def loop(self) -> None:
        while True:
            await asyncio.sleep(LOOP_S)
            try:
                await self.watch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reviewer watch failed")

    async def watch_once(self) -> None:
        if not self.runs:
            return
        run_id = self.runs.active_run_id()
        if not run_id:
            return
        run = self._run(run_id)
        if not run or run["reviewer_mode"] not in ("monitor", "gate") or run["waiting_until"]:
            return
        cfg = self.budgets.reviewer_config()
        now = utcnow().timestamp()
        last_t, last_id = self._last_watch.get(run_id, (parse(run["started_at"]).timestamp(), 0))  # type: ignore[union-attr]
        if now - last_t < cfg.interval_minutes * 60:
            return
        items = self.db.q("SELECT id, at, kind, text FROM timeline_items WHERE run_id=? AND id>? AND (simple=1 OR kind='tool') AND kind != 'reviewer' ORDER BY id",
                          (run_id, last_id))
        if not items:
            self._last_watch[run_id] = (now, last_id)
            return
        self._last_watch[run_id] = (now, items[-1]["id"])
        notes = [f"{i['at'][11:16]} {i['kind']}: {i['text']}" for i in items if i["kind"] != "tool"]
        tools = [i["text"] for i in items if i["kind"] == "tool"]
        body = "Watch update: what happened since your last look.\n\n" + ("\n".join(notes) or "(no notes)")
        if tools:
            body += f"\n\n{len(tools)} tool call(s), the last few:\n" + "\n".join(f"- {t}" for t in tools[-6:])
        self._add(run_id, "control", "watch", body, delivered=True)
        await self.turn(run_id, body, "watch")

    def undelivered(self) -> list[dict]:
        """Watch-turn replies not yet given to the agent (conv.deliver includes them)."""
        return self.db.q("SELECT * FROM reviewer_messages WHERE sender='reviewer' AND kind='reply' AND delivered_at IS NULL ORDER BY id")
