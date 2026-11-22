"""Presence: what this instance tells its friends it is up to (protocol/messages.md, kind `presence`).

The coarse state:
- **working**: a run is active;
- **asleep**: no run and nothing planned within the hour;
- **idle**: otherwise (no run, but a wakeup is near).
`next` is "tonight" when the next planned wakeup falls today (in the owner's time zone) or before 5 in the morning
tomorrow, else null. Friends the owner opted in for **exact** presence also get the wakeup's RFC 3339 time and, while
working, the run's status sentence as `now`; nobody else sees either.

It is sent to every friend (muting only silences what they send us) unless peer activity is paused: at once when the
state or `next` changes (or a friend's detail level does), and a change of the `now` sentence alone at most every
PRESENCE_INTERVAL_MINUTES. Nothing is repeated while it is unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta

from .auth import iso, parse
from .backends.base import BadRequest, Conflict

log = logging.getLogger("l0w.presence")
ASLEEP_AFTER = timedelta(hours=1)  # a wakeup within this long: idle, not asleep
TONIGHT_UNTIL = time(5, 0)  # "tonight" runs until 5 in the morning
NOW_MAX = 200
RETRY_AFTER = timedelta(minutes=1)  # after a send the gateway refused


class Presence:
    def __init__(self, db, settings, budgets, runs, friends):
        self.db, self.settings, self.budgets, self.runs, self.friends = db, settings, budgets, runs, friends
        self._failed_at: dict[str, datetime] = {}

    def snapshot(self, now: datetime) -> dict:
        """The instance's state, the next planned wakeup (a datetime or None) and the working run's status sentence."""
        run_id = self.runs.active_run_id()
        nxt = self.db.q1("SELECT at FROM wakeups WHERE status='planned' ORDER BY at LIMIT 1")
        next_at = parse(nxt["at"]) if nxt else None
        if run_id is not None:
            state = "working"
        elif next_at is None or next_at - now > ASLEEP_AFTER:
            state = "asleep"
        else:
            state = "idle"
        status = None
        if run_id is not None:
            row = self.db.q1("SELECT now_sentence FROM runs WHERE id=?", (run_id,))
            status = (" ".join((row["now_sentence"] or "").split())[:NOW_MAX] or None) if row else None
        return {"state": state, "next_at": next_at, "now": status}

    def tonight(self, next_at: datetime | None, now: datetime) -> bool:
        if next_at is None:
            return False
        local_now, local_next = self.budgets.local(now), self.budgets.local(next_at)
        if local_next.date() == local_now.date():
            return True
        return local_next.date() == local_now.date() + timedelta(days=1) and local_next.time() < TONIGHT_UNTIL

    def view(self, snap: dict, detail: str, now: datetime) -> dict:
        """The body one friend gets."""
        exact = detail == "exact"
        nxt = None
        if snap["next_at"] is not None:
            if exact:
                nxt = iso(snap["next_at"])
            elif self.tonight(snap["next_at"], now):
                nxt = "tonight"
        working = snap["state"] == "working"
        text = (self.friends.vault.redact(snap["now"]) if exact and working and snap["now"] else None) or None
        return {"state": snap["state"], "next": nxt, "now": text}

    async def tick(self, now: datetime) -> None:
        f = self.friends
        if f.paused() or not f.m.joined() or not f.m.gw:
            return
        rows = self.db.q("SELECT address, presence_detail, presence_sent_json, presence_sent_at FROM friends WHERE state='friends'")
        if not rows:
            return
        snap = self.snapshot(now)
        interval = timedelta(minutes=self.settings.get("PRESENCE_INTERVAL_MINUTES"))
        for r in rows:
            body = self.view(snap, r["presence_detail"], now)
            try:
                prev = json.loads(r["presence_sent_json"]) if r["presence_sent_json"] else None
            except ValueError:
                prev = None
            if prev == body:
                continue
            sent_at = parse(r["presence_sent_at"]) if r["presence_sent_at"] else None
            only_now = prev is not None and (prev.get("state"), prev.get("next")) == (body["state"], body["next"])
            if only_now and sent_at is not None and now - sent_at < interval:
                continue  # what it's doing now changed, but it was sent a moment ago
            failed = self._failed_at.get(r["address"])
            if failed is not None and now - failed < RETRY_AFTER:
                continue
            try:
                await f.send_presence(r["address"], body)
            except (BadRequest, Conflict) as e:
                self._failed_at[r["address"]] = now
                log.info("presence to %s not sent: %s", r["address"], getattr(e, "detail", e))
                continue
            self._failed_at.pop(r["address"], None)
            self.db.ex("UPDATE friends SET presence_sent_json=?, presence_sent_at=? WHERE address=?", (json.dumps(body), iso(now), r["address"]))
