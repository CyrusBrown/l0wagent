"""The spend breaker for peer activity (H5; docs/plans/hub.md "Outside content").

Friends' assistants can make ours spend: a message read, an answer written, a run woken. The breaker keeps
that bounded and asks the owner when it isn't. Nothing here is exact: it is an estimate from the usage ledger.

What counts as peer spend:
- A run a friend's wake request started (`peer_wakes.wakeup_id` = the run's wakeup): all of it.
- Inside any other run, "peer spans": when a friend's messages are delivered into a run, a span opens at the
  run's points then (`budgets.run_pts`). It closes at the run's points when the assistant next posts in a
  thread that isn't a peer thread, when the run ends, or `PEER_SPAN_MINUTES` after the last friend message.
  The spend is end minus start. A span is per (run, friend); friends whose spans overlap split the points
  between them evenly.

Limits (settings, read when used): `PEER_RUN_PCT` of one run's budget, `PEER_DAY_PCT` of the period's allowance
per local day. A friend's own switch (`friends.breaker = 'off'`) leaves their spend out of both, and lets the
assistant write to them while a limit is tripped. A run a friend woke is bounded by the budget of its wakeup,
which the owner allowed (`WAKE_FRIEND_PTS`), so it counts toward the day's limit but isn't held to the run limit.

When a limit is exceeded (checked when the assistant posts in a peer thread or asks to wake a friend, when a
span opens or closes, and in housekeeping): the assistant's posts in peer threads and its wake requests are
refused with 409 `peer_breaker`; presence keeps going (it isn't the assistant's spending). The assistant is told
once, the owner gets one notification and a card on the Friends page: Allow +5% today / Allow for this run (the
run limit only) / Stop for today (peer sends stay paused until local midnight, whatever the limits say).
The state (today's extra, exempt runs, stopped-until, the open card) is one JSON value in `kv`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .api import schemas as s
from .auth import iso, parse
from .backends.base import Conflict

if TYPE_CHECKING:
    from .friends import Friends

log = logging.getLogger("l0w.breaker")
KV = "peer_breaker"
ACTIVE = ("starting", "running")
EXEMPT_KEEP = 50
EPS = 1e-9


class Breaker:
    def __init__(self, friends: Friends):
        self.f = friends
        self.db = friends.db
        self.lock = threading.RLock()

    # ── time ───────────────────────────────────────────────────────────────
    def _local(self, now: datetime) -> datetime:
        return self.f.budgets.local(now)

    def day_start(self, now: datetime) -> datetime:
        """The start of the owner's local day, as UTC."""
        loc = self._local(now)
        return loc.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)

    def next_midnight(self, now: datetime) -> datetime:
        loc = self._local(now)
        return (loc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(now.tzinfo)  # wall-clock arithmetic: 00:00 tomorrow, DST or not

    # ── state ──────────────────────────────────────────────────────────────
    def _load(self, now: datetime) -> dict:
        row = self.db.q1("SELECT value FROM kv WHERE key=?", (KV,))
        st = json.loads(row["value"]) if row else {}
        st.setdefault("day", None)
        st.setdefault("extra_pct", 0.0)
        st.setdefault("exempt_runs", [])
        st.setdefault("stopped_until", None)
        st.setdefault("trip", None)
        st.setdefault("card", "closed")
        today = self._local(now).date().isoformat()
        stopped_ended = bool(st["stopped_until"]) and parse(st["stopped_until"]) <= now  # type: ignore[operator]
        if st["day"] != today or stopped_ended:
            if st["day"] != today:
                st["extra_pct"] = 0.0  # today's extra is for today only
                st["day"] = today
            if stopped_ended:
                st["stopped_until"] = None
                self.f.tell_agent("The owner's stop on replies to friends has ended. Replies to friends are allowed again, within the spending limits.")
            self._save(st)
        return st

    def _save(self, st: dict) -> None:
        st["exempt_runs"] = st["exempt_runs"][-EXEMPT_KEEP:]
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (KV, json.dumps(st), self.db.now()))

    def _changed(self) -> None:
        self.f.live.emit("friend", {"breaker": True})

    # ── what has been spent ────────────────────────────────────────────────
    def _counts(self, address: str) -> bool:
        row = self.db.q1("SELECT breaker FROM friends WHERE address=?", (address,))
        return not (row and row["breaker"] == "off")

    def wake_friend(self, run_id: int) -> str | None:
        """The friend whose wake request started this run, if one did."""
        row = self.db.q1("SELECT p.address FROM runs r JOIN peer_wakes p ON p.wakeup_id=r.wakeup_id "
                         "WHERE r.id=? AND p.direction='in' AND r.wakeup_id IS NOT NULL ORDER BY p.id LIMIT 1", (run_id,))
        return row["address"] if row else None

    def run_spend(self, run_id: int, since: str | None = None) -> dict[str, float]:
        """Points spent on each friend in one run (estimated), counting only what began at or after `since`."""
        wake = self.wake_friend(run_id)
        pts = self.f.budgets.run_pts
        if wake:
            run = self.db.q1("SELECT started_at FROM runs WHERE id=?", (run_id,))
            return {wake: pts(run_id)} if run and (since is None or run["started_at"] >= since) else {}
        spans = self.db.q("SELECT * FROM peer_spans WHERE run_id=? AND (? IS NULL OR started_at>=?)", (run_id, since, since))
        if not spans:
            return {}
        current: float | None = None
        ivs: list[tuple[str, float, float]] = []
        for sp in spans:
            end = sp["end_pts"]
            if end is None:
                current = pts(run_id) if current is None else current
                end = current
            ivs.append((sp["address"], sp["start_pts"], max(sp["start_pts"], end)))
        marks = sorted({p for _, a, b in ivs for p in (a, b)})
        out: dict[str, float] = {}
        for lo, hi in zip(marks, marks[1:], strict=False):  # each stretch is split evenly between the friends whose spans cover it
            who = {a for a, start, end in ivs if start <= lo and end >= hi}
            for a in who:
                out[a] = out.get(a, 0.0) + (hi - lo) / len(who)
        return out

    def run_spent(self, run_id: int) -> float:
        return sum(v for a, v in self.run_spend(run_id).items() if self._counts(a))

    def day_spend(self, now: datetime) -> dict[str, float]:
        """Points spent on each friend since local midnight, over every run (all friends, counted or not)."""
        since = iso(self.day_start(now))
        ids = {r["run_id"] for r in self.db.q("SELECT DISTINCT run_id FROM peer_spans WHERE started_at>=?", (since,))}
        ids |= {r["id"] for r in self.db.q("SELECT DISTINCT r.id FROM runs r JOIN peer_wakes p ON p.wakeup_id=r.wakeup_id WHERE p.direction='in' AND r.started_at>=?", (since,))}
        out: dict[str, float] = {}
        for rid in ids:
            for a, v in self.run_spend(rid, since).items():
                out[a] = out.get(a, 0.0) + v
        return out

    def _day_limit(self, st: dict, now: datetime) -> tuple[float, float, str]:
        """(points, percent, the period's unit): the percent of the period's allowance, today's extra included. A Dormant level has no allowance,
        so it is the reply budget that the share is taken of."""
        pid = self.f.budgets.role("worker").provider
        eff = self.f.budgets.effective(pid, now)
        base = eff.limits.allowance or eff.reply_pts
        pct = float(self.f.settings.get("PEER_DAY_PCT")) + float(st["extra_pct"])
        return base * pct / 100, pct, eff.period

    def _run_limit(self, run_id: int) -> tuple[float, float, float] | None:
        """(limit in points, percent, the run's budget), or None when the run has no budget to take a share of."""
        run = self.db.q1("SELECT budget FROM runs WHERE id=?", (run_id,))
        if not run or not run["budget"]:
            return None
        pct = float(self.f.settings.get("PEER_RUN_PCT"))
        return run["budget"] * pct / 100, pct, float(run["budget"])

    def _run_over(self, st: dict, run_id: int) -> tuple[float, float, float, float] | None:
        """(spent, limit, pct, budget) when this run has used more of its budget on friends than allowed."""
        if run_id in st["exempt_runs"] or self.wake_friend(run_id):
            return None
        lim = self._run_limit(run_id)
        if lim is None:
            return None
        spent = self.run_spent(run_id)
        return (spent, lim[0], lim[1], lim[2]) if spent > lim[0] + EPS else None

    def _active_runs(self) -> list[int]:
        return [r["id"] for r in self.db.q(f"SELECT id FROM runs WHERE status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id", ACTIVE)]

    # ── checking ───────────────────────────────────────────────────────────
    def _evaluate(self, st: dict, now: datetime, run_id: int | None) -> tuple[str | None, dict]:
        """Which limit is exceeded now (for this run and for the day): 'day', 'run' or None, with the numbers."""
        spend = self.day_spend(now)
        day_spent = sum(v for a, v in spend.items() if self._counts(a))
        day_limit, day_pct, unit = self._day_limit(st, now)
        info: dict = {"day_spent": day_spent, "day_limit": day_limit, "day_pct": day_pct, "unit": unit, "spend": spend}
        if day_spent > day_limit + EPS:
            return "day", info
        for rid in ([run_id] if run_id else self._active_runs()):
            over = self._run_over(st, rid)
            if over:
                info.update(run_id=rid, run_spent=over[0], run_limit=over[1], run_pct=over[2], run_budget=over[3])
                return "run", info
        return None, info

    def check(self, run_id: int | None = None, *, notify: bool = True, now: datetime | None = None) -> tuple[str | None, dict]:
        """Look at the limits; trip (once) when one is exceeded, and close the card when none is any more.
        -> ('stopped' | 'day' | 'run' | None, numbers). `run_id` is the run the assistant is posting from, when it is."""
        with self.lock:
            now = now or self.f.now()
            st = self._load(now)
            limit, info = self._evaluate(st, now, run_id)
            if st["stopped_until"]:
                return "stopped", info
            if limit:
                key = {"limit": limit, "run_id": info.get("run_id") if limit == "run" else None}
                trip = st["trip"] or {}
                if st["card"] != "open":
                    st["trip"] = {**key, "at": iso(now)}
                    st["card"] = "open"
                    self._save(st)
                    self._changed()
                    if notify:
                        self._announce(limit, info)
                elif (trip.get("limit"), trip.get("run_id")) != (key["limit"], key["run_id"]):
                    st["trip"] = {**key, "at": trip.get("at") or iso(now)}  # a different limit now: the same card, no second alarm
                    self._save(st)
                    self._changed()
            elif st["card"] == "open" and not run_id:  # nothing is over any more (the run ended, midnight passed, a limit was raised)
                st["card"], st["trip"] = "closed", None
                self._save(st)
                self._changed()
                self.f.tell_agent("Spending on friends is back within the limits. Replies to friends are allowed again.")
            return limit, info

    def _say(self, limit: str, info: dict) -> str:
        if limit == "run":
            return (f"run #{info['run_id']} spent about {info['run_spent']:.1f} pts on friends, more than {info['run_pct']:g}% "
                    f"({info['run_limit']:.1f} pts) of its {info['run_budget']:g} pts")
        return f"today's spending on friends is about {info['day_spent']:.1f} pts, more than {info['day_pct']:g}% ({info['day_limit']:.1f} pts) of the {info['unit']}'s allowance"

    def _announce(self, limit: str, info: dict) -> None:
        which = "this run's limit" if limit == "run" else "today's limit"
        until = "the owner decides" if limit == "run" else "the owner decides, or tomorrow"
        self.f.tell_agent(f"Spending on friends hit {which} ({self._say(limit, info)}); replies to friends are paused until {until}. "
                          "Your posts in friends' threads and your wake requests are refused meanwhile. Carry on with other work; the owner has been asked.")
        self.f.notifier.notify("friends", "Spending on friends hit a limit", f"{self._say(limit, info).capitalize()}. Replies to friends are paused until you decide.",
                               "/friends", "friend", "breaker")

    # ── the assistant's side ───────────────────────────────────────────────
    def gate(self, run_id: int | None, friend: dict | None = None) -> None:
        """May the assistant send something to a friend now (a post in a peer thread, a wake request)? Raises Conflict `peer_breaker`.
        A friend whose spend limit is off isn't held by a tripped limit, but the owner's Stop for today holds for everyone."""
        limit, info = self.check(run_id)
        if limit == "stopped":
            until = self.f.budgets.local(parse(self._load(self.f.now())["stopped_until"]))  # type: ignore[arg-type]
            raise Conflict("peer_breaker", f"The owner stopped replies to friends for today (until {until:%H:%M}), so nothing was sent.")
        if limit and not (friend and friend.get("breaker") == "off"):
            until = "the owner decides" if limit == "run" else "the owner decides, or tomorrow"
            raise Conflict("peer_breaker", f"Spending on friends hit the limit ({self._say(limit, info)}), so nothing was sent. Replies to friends are paused until {until}; "
                                           "the owner has been asked. Carry on with other work.")

    # ── spans ──────────────────────────────────────────────────────────────
    def on_delivered(self, run_id: int | None, addresses: list[str]) -> None:
        """Friends' messages were just delivered into this run: open (or extend) a span for each friend."""
        if not run_id or not addresses:
            return
        try:
            with self.lock:
                if self.wake_friend(run_id):
                    return  # a run a friend woke counts whole
                now = iso(self.f.now())
                pts = self.f.budgets.run_pts(run_id)
                for a in dict.fromkeys(addresses):
                    if self.db.ex("UPDATE peer_spans SET last_at=? WHERE run_id=? AND address=? AND ended_at IS NULL", (now, run_id, a)):
                        continue
                    self.db.ex("INSERT INTO peer_spans(run_id, address, start_pts, started_at, last_at) VALUES(?,?,?,?,?)", (run_id, a, pts, now, now))
            self.check()
        except Exception:  # noqa: BLE001  (delivery to the agent must not fail because of bookkeeping)
            log.exception("opening peer spans")

    def _close(self, sp: dict, by: str, now: datetime) -> None:
        self.db.ex("UPDATE peer_spans SET end_pts=?, ended_at=?, closed_by=? WHERE id=? AND ended_at IS NULL",
                   (self.f.budgets.run_pts(sp["run_id"]), iso(now), by, sp["id"]))

    def close_run_spans(self, run_id: int, by: str) -> None:
        try:
            with self.lock:
                spans = self.db.q("SELECT * FROM peer_spans WHERE run_id=? AND ended_at IS NULL", (run_id,))
                for sp in spans:
                    self._close(sp, by, self.f.now())
            if spans:
                self.check()
        except Exception:  # noqa: BLE001
            log.exception("closing peer spans")

    def on_nonpeer_post(self, run_id: int | None) -> None:
        """The assistant posted in a thread that isn't a peer thread: whatever it spent on friends before is closed."""
        if run_id:
            self.close_run_spans(run_id, "post")

    def on_run_end(self, run_id: int) -> None:
        self.close_run_spans(run_id, "run_end")

    def housekeeping(self, now: datetime) -> None:
        try:
            with self.lock:
                cut = iso(now - timedelta(minutes=int(self.f.settings.get("PEER_SPAN_MINUTES"))))
                for sp in self.db.q("SELECT sp.*, r.status AS rstatus FROM peer_spans sp LEFT JOIN runs r ON r.id=sp.run_id WHERE sp.ended_at IS NULL"):
                    if sp["rstatus"] not in ACTIVE:
                        self._close(sp, "run_end", now)
                    elif sp["last_at"] <= cut:
                        self._close(sp, "timeout", now)
                self.db.ex("DELETE FROM peer_spans WHERE ended_at IS NOT NULL AND ended_at<?", (iso(now - timedelta(days=30)),))
            self.check(now=now)
        except Exception:  # noqa: BLE001  (the rest of housekeeping must still run)
            log.exception("breaker housekeeping")

    # ── the owner's side ───────────────────────────────────────────────────
    def view(self) -> s.FriendBreaker:
        with self.lock:
            now = self.f.now()
            st = self._load(now)
            limit, info = self._evaluate(st, now, None)
            tripped = st["card"] == "open"
            stopped = parse(st["stopped_until"]) if st["stopped_until"] else None
            names = {r["address"]: r["display_name"] for r in self.db.q("SELECT address, display_name FROM friends")}
            people = [s.FriendBreakerPerson(address=a, display_name=names.get(a, a), pts=round(v, 2)) for a, v in sorted(info["spend"].items(), key=lambda kv: -kv[1]) if v > 0]
            trip = st["trip"] or {}
            shown = trip.get("limit") if tripped else None
            run_id = trip.get("run_id") if shown == "run" else None
            if shown == "run" and run_id is not None and (over := self._run_over(st, run_id)):
                info.update(run_id=run_id, run_spent=over[0], run_limit=over[1], run_pct=over[2], run_budget=over[3])
            run_known = shown == "run" and "run_spent" in info and info.get("run_id") == run_id
            if tripped and shown == "day":
                text = (f"Your assistant's spending on friends today is about {info['day_spent']:.1f} pts, more than {info['day_pct']:g}% "
                        f"({info['day_limit']:.1f} pts) of the {info['unit']}'s allowance")
                if people:
                    text += " (" + ", ".join(f"{p.display_name} {p.pts:g}" for p in people[:4]) + ")"
                text += ". Its replies to friends and wake requests are paused."
            elif tripped and run_known:
                text = (f"Run #{run_id} has spent about {info['run_spent']:.1f} pts on friends, more than {info['run_pct']:g}% ({info['run_limit']:.1f} pts) of its budget of "
                        f"{info['run_budget']:g} pts. Its replies to friends and wake requests are paused.")
            elif tripped:
                text = "A spending limit on friends was hit. Replies to friends and wake requests are paused."
            elif stopped:
                text = "You stopped replies to friends for today."
            else:
                text = ""
            choices: list[str] = (["allow_today"] + (["allow_run"] if shown == "run" and run_id is not None else []) + ["stop_today"]) if tripped and not stopped else []
            return s.FriendBreaker(
                tripped=tripped, paused=tripped or bool(stopped), limit=shown, explanation=text, day_spent=round(info["day_spent"], 2), day_limit=round(info["day_limit"], 2),
                day_pct=info["day_pct"], day_step_pct=float(self.f.settings.get("PEER_DAY_PCT_STEP")), run_id=run_id,
                run_spent=round(info["run_spent"], 2) if run_known else None, run_limit=round(info["run_limit"], 2) if run_known else None,
                stopped_until=stopped, choices=choices, spent_on=people, at=parse(trip["at"]) if tripped and trip.get("at") else None)  # type: ignore[arg-type]

    def _decide(self, change: str) -> s.FriendBreaker:
        """Apply the owner's choice, then look again: a limit that is still exceeded keeps the card (no second alarm)."""
        now = self.f.now()
        st = self._load(now)
        run_id = (st["trip"] or {}).get("run_id") if st["card"] == "open" and (st["trip"] or {}).get("limit") == "run" else None
        if change == "allow_today":
            st["extra_pct"] += float(self.f.settings.get("PEER_DAY_PCT_STEP"))
            said = f"The owner allowed {float(self.f.settings.get('PEER_DAY_PCT_STEP')):g}% more for friends today. Replies to friends are allowed again, until the new limit."
        elif change == "allow_run":
            if run_id is None:
                raise Conflict("not_tripped", "No run is over its limit, so there is nothing to allow.")
            st["exempt_runs"].append(run_id)
            said = f"The owner allowed run #{run_id} to spend more on friends. Replies to friends are allowed again in this run."
        else:
            until = self.next_midnight(now)
            st["stopped_until"] = iso(until)
            said = (f"The owner stopped replies to friends for today (until {self.f.budgets.local(until):%H:%M}). "
                    "Your posts in friends' threads and your wake requests are refused until then.")
        st["card"], st["trip"] = "closed", None
        self._save(st)
        self.f.tell_agent(said)
        self._changed()
        if change != "stop_today":
            reopened, info = self._evaluate(st, now, None)
            if reopened:  # still over: the card stays, quietly
                st["card"], st["trip"] = "open", {"limit": reopened, "run_id": info.get("run_id") if reopened == "run" else None, "at": iso(now)}
                self._save(st)
        return self.view()

    def allow_today(self) -> s.FriendBreaker:
        with self.lock:
            return self._decide("allow_today")

    def allow_run(self) -> s.FriendBreaker:
        with self.lock:
            return self._decide("allow_run")

    def stop_today(self) -> s.FriendBreaker:
        with self.lock:
            return self._decide("stop_today")
