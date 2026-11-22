"""Friends and peer messages (H5, part 1). The spec is protocol/messages.md.

Who holds what: the gateway holds everything cryptographic about friends (pinned keys, delivery tokens,
sequence numbers, the outbox that retries); control holds what the owner decides: the friends and their
settings, requests as owner cards, friend codes (SHA-256 of the secret only), peer threads and messages.

Inbound (`receive`, behind `POST /api/peer/v0/inbox`, reachable only from the hub tunnel): the raw bytes go
to the gateway's `/v0/peer/open`, which checks token, signature, time window and seal and returns the plain
body without changing any state. Control stores the result idempotently on (sender, envelope id) in one
transaction, and only then answers `202`; a retry of the same envelope is a `200 duplicate`.

Outbound: `/v0/peer/send` (the gateway seals, signs, tries once, and keeps retrying); `housekeeping` reads
the outbox states back and tells the owner about final failures.

Everything a peer sends is untrusted data, like a fetched web page: text goes through `vault.redact`, files
land in `_inbox/peers/<address>/` the way the owner's uploads land in `_inbox/`, and a friend request never
reaches the agent: it is a card for the owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import quote

from l0wproto import b64, messages
from l0wproto import payloads as P
from l0wproto.consts import MAX_FILES
from l0wproto.errors import ProtocolError
from l0wproto.payloads import build_file, split_address

from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, Conflict, NotFound
from .breaker import Breaker
from .membership import GatewayError

log = logging.getLogger("l0w.friends")

DEDUP_DAYS = 9  # the spec: receivers keep the ids they've seen for at least 9 days
REQUEST_EXPIRE_DAYS = 30
KEY_CHECK_EVERY = timedelta(hours=24)
TEXT_MAX = 50_000
NOTE_MAX = 1000
# Inbound limits (the spec's friend-request limits are counted in the database; these are about volume).
OVERALL_PER_MIN, SENDER_PER_MIN = 600, 120
REQUESTS_PER_DAY_SENDER, REQUESTS_PER_DAY_OVERALL = 10, 100
PEER_KINDS = ("friend-request", "friend-accept", "friend-decline", "unfriend", "message", "presence", "wake-request", "wake-reply", "same-owner")
SERVICE_KINDS = tuple(P.SERVICE_KINDS)  # hub services' envelopes (hubservices.py)
REQUESTS_FROM = ("anyone", "hub", "codes")
# gateway error code -> HTTP status we answer the sender with (protocol/messages.md "Errors")
OPEN_ERRORS = {"no_token": 403, "bad_signature": 401, "not_here": 404, "bad_seal": 400, "bad_body": 400, "bad_document": 400, "unknown_kind": 422,
               "too_large": 413, "unresolvable": 503}
FILE_NAME_BAD = re.compile(r"[^A-Za-z0-9._@+ -]")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


class Reject(Exception):
    """Answer the sender with this instead of storing the envelope."""

    def __init__(self, status: int, error: str, detail: str = "", retry_after: int | None = None):
        super().__init__(error)
        self.status, self.error, self.detail, self.retry_after = status, error, detail, retry_after


class Window:
    """At most `limit` hits per `seconds` for each key; in memory (a restart forgives)."""

    def __init__(self, limit: int, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self.hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> int | None:
        """Record a hit; None if allowed, else how many seconds to wait."""
        now = self.clock()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] >= self.seconds:
            q.popleft()
        if len(q) >= self.limit:
            return max(1, int(self.seconds - (now - q[0])) + 1)
        q.append(now)
        if len(self.hits) > 5000:  # forget idle senders
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= self.seconds]:
                self.hits.pop(k, None)
        return None


def _error_body(error: str, detail: str = "") -> dict:
    return {"v": "l0w/0.1", "error": error, "detail": detail}


def folder_name(address: str) -> str:
    """A friend's folder under _inbox/peers/: the address with anything odd replaced."""
    name = re.sub(r"[^a-z0-9@._-]", "_", address.lower()).lstrip(".")
    return name[:100] or "friend"


def safe_file_name(name: object) -> str:
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    base = FILE_NAME_BAD.sub("_", "".join(c for c in base if c.isprintable()))[:120].strip()
    return base or "file"


def code_hash(secret_text: str) -> str:
    """What is kept of a friend code's secret: SHA-256 of the secret as it travels in a request's `code`."""
    return hashlib.sha256(secret_text.encode()).hexdigest()


class Friends:
    def __init__(self, db, live, conv, vault, settings, membership, notifier, write_inbox: Callable[[str, str, bytes], str],
                 now: Callable[[], datetime] = utcnow):
        self.db, self.live, self.conv, self.vault, self.settings = db, live, conv, vault, settings
        self.m, self.notifier, self.write_inbox, self.now = membership, notifier, write_inbox, now
        self.scheduler = None  # set by the backend: waking plans a wakeup through it
        self.budgets = None  # set by the backend: the worker provider, the owner's time zone
        self.storage = None  # set by the backend: files.Storage, how a message's files are read (no symlink followed)
        self.hubservices = None  # set by the backend: takes the `service-*` envelopes (hubservices.py)
        self.breaker = Breaker(self)  # the spend breaker (breaker.py): the gate on the assistant's sends, peer spans
        self.overall = Window(OVERALL_PER_MIN, 60)
        self.per_sender = Window(SENDER_PER_MIN, 60)
        self._inflight: set[int] = set()
        self._last_slow: datetime | None = None
        live.getters.update(friend=lambda a: self.friend(a), friend_request=lambda i: self.request_model(self._request_row(int(i))))

    # ── small helpers ──────────────────────────────────────────────────────
    def r(self, text: str | None) -> str:
        return self.vault.redact(text) or ""

    def _notify(self, address: str, title: str, body: str, url: str, ref_kind: str = "friend") -> None:
        row = self._friend_row(address)
        if row and row["muted"]:
            return
        self.notifier.notify("friends", title, body, url, ref_kind, address)

    def _changed(self, address: str, *, deleted: bool = False) -> None:
        self.live.changed("friend", address, deleted=deleted)

    def paused(self) -> bool:
        return bool(self.settings.get("PEER_PAUSED"))

    def requests_from(self) -> str:
        v = str(self.settings.get("FRIEND_REQUESTS_FROM"))
        return v if v in REQUESTS_FROM else "hub"

    def _need_joined(self) -> dict:
        r = self.m.joined()
        if not r or not self.m.gw:
            raise Conflict("not_joined", "Join a hub first: friends are other members of hubs.")
        return r

    def _address(self, text: str) -> str:
        try:
            name, domain = split_address(text.strip())
        except ProtocolError as e:
            raise BadRequest("bad_address", f"That isn't an address (name@hub-domain): {e}") from e
        return f"{name}@{domain}"

    # ── views ──────────────────────────────────────────────────────────────
    def _friend_row(self, address: str) -> dict | None:
        return self.db.q1("SELECT * FROM friends WHERE address=?", (address,))

    def friend_model(self, r: dict) -> s.Friend:
        presence = None
        if r["presence_json"] and r["presence_at"]:
            try:
                p = json.loads(r["presence_json"])
                presence = s.FriendPresence(state=p["state"], next=p.get("next"), now=p.get("now"), at=parse(r["presence_at"]))  # type: ignore[arg-type]
            except (ValueError, KeyError, TypeError):
                presence = None
        return s.Friend(address=r["address"], display_name=r["display_name"], fingerprint=r["fingerprint"], state=r["state"], share=r["share"],
                        presence_detail=r["presence_detail"], waking=r["waking"], muted=bool(r["muted"]), breaker=r["breaker"], thread_id=r["thread_id"], presence=presence,
                        same_owner=bool(r["same_owner_json"]), key_alarm=r["key_alarm"], created_at=parse(r["created_at"]),  # type: ignore[arg-type]
                        updated_at=parse(r["updated_at"]))  # type: ignore[arg-type]

    def friend(self, address: str) -> s.Friend:
        r = self._friend_row(address.lower())
        if not r:
            raise NotFound(f"friend {address}")
        return self.friend_model(r)

    def friends(self) -> list[s.Friend]:
        order = "CASE state WHEN 'requested_in' THEN 0 WHEN 'friends' THEN 1 WHEN 'requested_out' THEN 2 ELSE 3 END"
        return [self.friend_model(r) for r in self.db.q(f"SELECT * FROM friends ORDER BY {order}, display_name COLLATE NOCASE")]

    def _request_row(self, rid: int) -> dict:
        r = self.db.q1("SELECT * FROM friend_requests WHERE id=?", (rid,))
        if not r:
            raise NotFound(f"friend request {rid}")
        return r

    def request_model(self, r: dict) -> s.FriendRequest:
        return s.FriendRequest(id=r["id"], direction=r["direction"], address=r["address"], note=r["note"], made_by=r["made_by"], code_used=bool(r["code_used"]),
                               fingerprint=r["fingerprint"], state=r["state"], error=r["error"], created_at=parse(r["created_at"]),  # type: ignore[arg-type]
                               decided_at=parse(r["decided_at"]))

    def requests(self, state: str | None = None) -> list[s.FriendRequest]:
        rows = self.db.q("SELECT * FROM friend_requests WHERE (? IS NULL OR state=?) ORDER BY id DESC LIMIT 200", (state, state))
        return [self.request_model(r) for r in rows]

    def _req_changed(self, rid: int) -> None:
        self.live.changed("friend_request", rid)
        self.live.emit("inbox_counts")

    def settings_view(self) -> s.FriendSettings:
        return s.FriendSettings(requests_from=self.requests_from(), paused=self.paused())  # type: ignore[arg-type]

    def set_settings(self, req: s.FriendSettingsPatch) -> s.FriendSettings:
        if req.requests_from is not None:
            self.settings.set("FRIEND_REQUESTS_FROM", req.requests_from)
        if req.paused is not None:
            self.settings.set("PEER_PAUSED", req.paused)
        self.live.emit("status")
        return self.settings_view()

    def patch(self, address: str, req: s.FriendPatch) -> s.Friend:
        address = address.lower()
        row = self._friend_row(address)
        if not row:
            raise NotFound(f"friend {address}")
        sets: dict = {}
        if req.display_name is not None:
            sets["display_name"] = self.r(req.display_name.strip())[:80] or address
        for k in ("share", "presence_detail", "waking", "breaker"):
            if getattr(req, k) is not None:
                sets[k] = getattr(req, k)
        if req.muted is not None:
            sets["muted"] = int(req.muted)
        if sets:
            self.db.ex(f"UPDATE friends SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE address=?", (*sets.values(), self.db.now(), address))
            self._changed(address)
            if "breaker" in sets:  # what counts toward the limits changed: look again
                self.breaker.check(notify=True)
                self.breaker._changed()
        return self.friend(address)

    # ── the gateway ────────────────────────────────────────────────────────
    async def _g(self, method: str, path: str, body: dict | None = None) -> dict:
        gw = self.m.gw
        if gw is None:
            raise BadRequest("no_gateway", "This install has no gateway service, so it can't have friends. Update with ./l0w update.")
        try:
            return await gw.call(method, path, body)
        except GatewayError as e:
            raise self._map(e) from e

    @staticmethod
    def _map(e: GatewayError) -> BadRequest | Conflict:
        d = e.detail
        match e.error:
            case "key_changed":
                return Conflict("key_changed", "Their key changed with no rotation from the one we pinned. Don't trust it until you've checked with them (compare fingerprints).")
            case "fingerprint_mismatch":
                return Conflict("fingerprint_mismatch", "Their key doesn't match the fingerprint in the code. Check the code with them before going on.")
            case "hub_key_changed":
                return Conflict("hub_key_changed", "Their hub's signing key changed with no rotation. Don't trust it until you've checked with the hub's operator.")
            case "no_such_member":
                return BadRequest("no_such_member", "Nobody has that name on that hub.")
            case "unreachable" | "hub_capabilities" | "bad_directory" | "gateway_unreachable":
                return BadRequest("unreachable", f"Couldn't reach or verify them just now ({e.error}). Try again in a moment.")
            case "no_tunnel":
                return Conflict("not_connected", "This instance isn't connected to its hub yet.")
            case "not_pinned" | "no_token":
                return Conflict(e.error, f"Not friends yet ({e.error}).")
            case "too_large":
                return BadRequest("too_large", "That is too large to send.")
            case _:
                return BadRequest("gateway", f"The gateway refused ({e.error}{': ' + d if d else ''}).")

    async def _pin(self, address: str, fingerprint: str | None = None, held_token: dict | None = None) -> dict:
        body: dict = {"address": address}
        if fingerprint:
            body["fingerprint"] = fingerprint
        if held_token:
            body["held_token"] = held_token
        try:
            r = await self._g("POST", "/v0/peer/pin", body)
        except Conflict as e:
            if e.error == "key_changed":
                self._alarm(address, e.detail)
            raise
        return r

    def _alarm(self, address: str, text: str) -> None:
        row = self._friend_row(address)
        if row and not row["key_alarm"]:
            self.db.ex("UPDATE friends SET key_alarm=?, updated_at=? WHERE address=?", (text, self.db.now(), address))
            self._changed(address)
            self.notifier.notify("friends", f"{row['display_name']}'s key changed", "Their key changed with no rotation. Check with them before you trust it.",
                                 "/friends", "friend", address)

    @staticmethod
    def _transient(e: Exception) -> bool:
        """The gateway or the hub connection is away: a message waits and is tried again; nothing else does."""
        return (isinstance(e, BadRequest) and e.error == "unreachable") or (isinstance(e, Conflict) and e.error == "not_connected")

    async def _send(self, to: str, kind: str, body: dict, *, request_token: bool = False, message_id: int | None = None,
                    request_id: int | None = None, outbox_id: int | None = None) -> dict:
        """Hand one envelope to the gateway and record it in peer_outbox. Raises when the gateway refuses it."""
        if self.paused():
            raise Conflict("paused", "Peer activity is paused (Settings → Friends).")
        now = self.db.now()
        oid = outbox_id or self.db.ex("INSERT INTO peer_outbox(to_address, kind, message_id, request_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                                      (to, kind, message_id, request_id, now, now))
        self._inflight.add(oid)
        try:
            try:
                r = await self._g("POST", "/v0/peer/send", {"to": to, "kind": kind, "body": body, **({"request_token": True} if request_token else {})})
            except (BadRequest, Conflict) as e:
                if not (kind == "message" and self._transient(e)):  # a message waits for the gateway; everything else is the owner's to retry
                    self.db.ex("UPDATE peer_outbox SET state='failed', error=?, updated_at=? WHERE id=?", ((getattr(e, "detail", "") or e.error)[:300], self.db.now(), oid))
                raise
            self.db.ex("UPDATE peer_outbox SET envelope_id=?, updated_at=? WHERE id=?", (r["id"], self.db.now(), oid))
            self._outcome(self.db.q1("SELECT * FROM peer_outbox WHERE id=?", (oid,)), r.get("state", "queued"), r.get("error"))  # type: ignore[arg-type]
            return r
        finally:
            self._inflight.discard(oid)

    def _outcome(self, ob: dict, state: str, error: str | None) -> None:
        """The gateway's word on one envelope; acts only when the state changes (the outbox feed repeats entries)."""
        if state not in ("queued", "delivered", "failed") or ob["state"] == state or ob["state"] in ("delivered", "failed"):
            return
        self.db.ex("UPDATE peer_outbox SET state=?, error=?, updated_at=? WHERE id=?", (state, error[:300] if error else None, self.db.now(), ob["id"]))
        to = ob["to_address"]
        if state == "delivered":
            if ob["message_id"]:
                self.db.ex("UPDATE messages SET delivery='delivered' WHERE id=? AND delivery='queued'", (ob["message_id"],))
                self.live.changed("message", ob["message_id"])
            return
        if state != "failed":
            return
        why = f" ({error})" if error else ""
        friend = self._friend_row(to)
        name = friend["display_name"] if friend else to
        if ob["kind"] == "message" and ob["message_id"]:
            m = self.db.q1("SELECT thread_id FROM messages WHERE id=?", (ob["message_id"],))
            if m:
                self.conv.system_message(m["thread_id"], f"Couldn't deliver message #{ob['message_id']} to {name}{why}.")
                if self.db.q1("SELECT 1 FROM messages WHERE id=? AND sender='assistant'", (ob["message_id"],)):  # the agent hears about its own
                    self.tell_agent(f"Your message #{ob['message_id']} to {name} ({to}) wasn't delivered{why}.")
            self._notify(to, f"Message to {name} wasn't delivered", f"Delivery failed{why}.", f"/t/{m['thread_id']}" if m else "/friends", "message")
        elif ob["kind"] == "friend-request" and ob["request_id"]:
            self.db.ex("UPDATE friend_requests SET state='failed', error=?, decided_at=? WHERE id=?", (error or "not delivered", self.db.now(), ob["request_id"]))
            self.db.ex("DELETE FROM friends WHERE address=? AND state='requested_out'", (to,))
            self._req_changed(ob["request_id"])
            self._changed(to, deleted=True)
            self.notifier.notify("friends", f"Your friend request to {to} wasn't delivered", f"Delivery failed{why}.", "/friends", "friend", to)
        elif ob["kind"] == "friend-accept" and ob["request_id"]:
            self.db.ex("UPDATE friend_requests SET state='failed', error=?, decided_at=? WHERE id=?", (error or "not delivered", self.db.now(), ob["request_id"]))
            self.db.ex("DELETE FROM friends WHERE address=? AND state='friends'", (to,))
            self._req_changed(ob["request_id"])
            self._changed(to, deleted=True)
            self.notifier.notify("friends", f"Your acceptance to {to} wasn't delivered", f"Delivery failed{why}. Ask them to send a new request.", "/friends", "friend", to)

    # ── threads ────────────────────────────────────────────────────────────
    def _thread_for(self, address: str) -> int:
        """The peer thread for a friend (made once; a returning friend gets their old thread back)."""
        row = self._friend_row(address)
        if row and row["thread_id"]:
            return row["thread_id"]
        t = self.db.q1("SELECT id FROM threads WHERE type='peer' AND title=? ORDER BY id LIMIT 1", (address,))
        if t:
            tid = t["id"]
        else:
            now = self.db.now()
            tid = self.db.ex("INSERT INTO threads(project_id, title, type, created_by, created_at, updated_at) VALUES(NULL,?,'peer','owner',?,?)", (address, now, now))
            self.db.ex("UPDATE threads SET last_read_message_id=(SELECT COALESCE(MAX(id),0) FROM messages) WHERE id=?", (tid,))
        if row:
            self.db.ex("UPDATE friends SET thread_id=? WHERE address=?", (tid, address))
        self.conv._touch_thread(tid)
        return tid

    # ── what the assistant may do with friends ─────────────────────────────
    def tell_agent(self, text: str) -> None:
        """A note from control for the agent's next inbox check (never text that came from a friend, except as quoted data)."""
        self.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(NULL, ?, ?)", (f"[control] {text}", self.db.now()))

    def gate_message(self, thread_id: int, run_id: int | None = None) -> None:
        """May the assistant write in this peer thread? Raises Conflict (and nothing is stored) when the message can't go out."""
        f = self.db.q1("SELECT * FROM friends WHERE thread_id=?", (thread_id,))
        if f is None or f["state"] not in ("friends", "blocked"):
            raise Conflict("not_friends", "You're not friends with them (any more), so nothing was sent.")
        if f["state"] == "blocked":
            raise Conflict("blocked", "The owner blocked them, so nothing was sent.")
        if self.paused():
            raise Conflict("paused", "The owner paused all peer activity (Settings → Friends), so nothing was sent.")
        self.breaker.gate(run_id, f)  # 409 peer_breaker when the spend limits say stop

    def agent_view(self) -> list[dict]:
        """The friends the assistant may know about: friends, and people it asked (drafts and sent requests). Nothing secret:
        no codes, tokens or keys. Requests to the owner and blocked people are not its business. Presence `now` is the friend's own text."""
        rows = self.db.q("SELECT * FROM friends WHERE state IN ('friends','requested_out') ORDER BY CASE state WHEN 'friends' THEN 0 ELSE 1 END, display_name COLLATE NOCASE")
        out = []
        for r in rows:
            view = {"address": r["address"], "name": r["display_name"], "state": r["state"], "share": r["share"], "thread": r["thread_id"], "presence": None}
            if r["state"] == "friends" and r["presence_json"]:
                try:
                    p = json.loads(r["presence_json"])
                    view["presence"] = {"state": p.get("state"), "next": p.get("next"), "now": p.get("now"), "at": r["presence_at"]}
                except ValueError:
                    pass
            out.append(view)
        pending = self.db.q("SELECT address, note FROM friend_requests WHERE direction='out' AND state='pending' AND made_by='assistant' ORDER BY id")
        for r in pending:
            out.append({"address": r["address"], "name": r["address"], "state": "draft", "share": None, "thread": None, "presence": None})
        return out

    # ── friend codes ───────────────────────────────────────────────────────
    async def create_code(self, req: s.FriendCodeCreate) -> s.FriendCodeCreated:
        me = self._need_joined()
        if req.idempotency_key and self.db.q1("SELECT 1 FROM friend_codes WHERE client_id=?", (req.idempotency_key,)):
            # Only the secret's hash is kept, so a repeat can't show the code again: make a new one (and delete the lost one).
            raise Conflict("code_already_made", "That code was made, but it's shown only once. Make a new one (and delete this one).")
        fp = (await self._g("GET", "/v0/peer/fingerprint"))["fingerprint"]
        text, secret = messages.make_friend_code(me["address"], fp)
        now = self.now()
        expires = iso(now + timedelta(days=req.expires_days or self.settings.get("FRIEND_CODE_EXPIRE_DAYS")))
        try:
            cid = self.db.ex("INSERT INTO friend_codes(secret_sha256, label, client_id, created_at, expires_at) VALUES(?,?,?,?,?)",
                             (code_hash(b64.encode(secret)), self.r(req.label.strip())[:80], req.idempotency_key, iso(now), expires))
        except sqlite3.IntegrityError as e:
            raise Conflict("code_already_made", "That code was made, but it's shown only once.") from e
        return s.FriendCodeCreated(id=cid, code=text, address=me["address"], fingerprint=fp, expires_at=parse(expires))  # type: ignore[arg-type]

    def codes(self) -> list[s.FriendCode]:
        now = iso(self.now())
        out = []
        for r in self.db.q("SELECT * FROM friend_codes ORDER BY id DESC LIMIT 100"):
            state = "used" if r["used_at"] else "expired" if r["expires_at"] <= now else "active"
            out.append(s.FriendCode(id=r["id"], label=r["label"], state=state, created_at=parse(r["created_at"]), expires_at=parse(r["expires_at"]),  # type: ignore[arg-type]
                                    used_at=parse(r["used_at"]), used_by=r["used_by"]))
        return out

    def delete_code(self, code_id: int) -> None:
        if not self.db.ex("DELETE FROM friend_codes WHERE id=?", (code_id,)):
            raise NotFound(f"friend code {code_id}")

    async def fingerprint(self) -> s.FriendFingerprint:
        me = self._need_joined()
        return s.FriendFingerprint(address=me["address"], fingerprint=(await self._g("GET", "/v0/peer/fingerprint"))["fingerprint"])

    def _claim_code(self, secret_text: object, by: str) -> bool:
        """Spend a valid, unused, unexpired code. The same secret is never good twice."""
        if not isinstance(secret_text, str) or not secret_text:
            return False
        now = self.db.now()
        return bool(self.db.ex("UPDATE friend_codes SET used_at=?, used_by=? WHERE secret_sha256=? AND used_at IS NULL AND expires_at>?",
                               (now, by, code_hash(secret_text), now)))

    # ── asking someone to be friends ───────────────────────────────────────
    async def request(self, req: s.FriendRequestCreate) -> s.FriendRequest:
        return await self._new_request(req.address, req.note, req.code, "owner", send=True, key=req.idempotency_key)

    async def draft_request(self, address: str, note: str = "", code: str | None = None) -> s.FriendRequest:
        """The assistant's draft: waits for its owner's approval before anything is sent. Never sends, and never approves."""
        cap = self.settings.get("MAX_OPEN_ASKS_PER_RUN")
        if self.db.q1("SELECT COUNT(*) AS n FROM friend_requests WHERE direction='out' AND made_by='assistant' AND state='pending'")["n"] >= cap:
            raise Conflict("too_many_drafts", f"{cap} friend requests of yours are already waiting for the owner. Wait for their answer before drafting more.")
        if self.db.q1("SELECT 1 FROM friend_requests WHERE direction='out' AND state='pending' AND address=?", (self._address(address),)):
            raise Conflict("already_drafted", f"A request to {address} is already waiting for the owner.")
        return await self._new_request(address, note, code, "assistant", send=False, key=None)

    async def _new_request(self, address: str | None, note: str, code: str | None, by: str, *, send: bool, key: str | None) -> s.FriendRequest:
        me = self._need_joined()
        fp = None
        if code and code.strip():
            try:
                code_addr, fp, _ = messages.parse_friend_code(code.strip())
            except ProtocolError as e:
                raise BadRequest("bad_friend_code", "That isn't a friend code (it starts with l0wf1:).") from e
            if address and self._address(address) != code_addr:
                raise BadRequest("code_mismatch", f"That code is for {code_addr}, not {address}.")
            address = code_addr
        if not address:
            raise BadRequest("address_needed", "Give an address (name@hub-domain) or a friend code.")
        address = self._address(address)
        if address == me["address"]:
            raise BadRequest("self", "That's your own address.")
        if key:
            prior = self.db.q1("SELECT * FROM friend_requests WHERE client_id=?", (key,))
            if prior:
                return self.request_model(prior)
        self._can_ask(address)
        note = self.r(note.strip())[:NOTE_MAX]
        try:
            rid = self.db.ex("INSERT INTO friend_requests(direction, address, note, made_by, code_used, code, fingerprint, state, client_id, created_at) "
                             "VALUES('out',?,?,?,?,?,?,'pending',?,?)", (address, note, by, int(bool(code)), (code or "").strip() or None, fp, key, self.db.now()))
        except sqlite3.IntegrityError:  # the same key, twice at once
            return self.request_model(self.db.q1("SELECT * FROM friend_requests WHERE client_id=?", (key,)))  # type: ignore[arg-type]
        self._req_changed(rid)
        if send:
            try:
                await self._send_request(rid)
            except (BadRequest, Conflict):  # refused on the spot (no such member, wrong fingerprint...): nothing to keep
                self.db.ex("UPDATE peer_outbox SET request_id=NULL WHERE request_id=?", (rid,))
                self.db.ex("DELETE FROM friend_requests WHERE id=?", (rid,))
                self.live.changed("friend_request", rid, deleted=True)
                raise
        else:
            self.notifier.notify("approval", "Your assistant wants to ask someone to be friends", address, "/friends", "friend_request", str(rid))
        return self.request_model(self._request_row(rid))

    def _can_ask(self, address: str) -> None:
        f = self._friend_row(address)
        if f and f["state"] == "friends":
            raise Conflict("already_friends", f"You and {address} are already friends.")
        if f and f["state"] == "blocked":
            raise Conflict("blocked", f"You blocked {address}. Unblock them first.")
        if f and f["state"] == "requested_in":
            raise Conflict("they_asked_first", f"{address} already asked you: approve their request.")
        if f and f["state"] == "requested_out":
            raise Conflict("already_requested", f"You already asked {address}; wait for their answer.")

    async def _send_request(self, rid: int) -> None:
        """Send an outgoing request (pinning them first for a draft). The friend row exists before the send:
        their accept can arrive while we're still waiting for the gateway."""
        req = self._request_row(rid)
        address = req["address"]
        secret = None
        if req["code"]:
            _, fp, sec = messages.parse_friend_code(req["code"])
            secret = b64.encode(sec)
        else:
            fp = None
        pin = await self._pin(address, fp)
        now = self.db.now()
        self._can_ask(address)
        self.db.ex("INSERT INTO friends(address, display_name, fingerprint, state, created_at, updated_at) VALUES(?,?,?,'requested_out',?,?)",
                   (address, address, pin["fingerprint"], now, now))
        self.db.ex("UPDATE friend_requests SET state='sent', fingerprint=?, code=NULL WHERE id=?", (pin["fingerprint"], rid))
        self._changed(address)
        self._req_changed(rid)
        try:
            r = await self._send(address, "friend-request", {"code": secret, "note": req["note"], "by": req["made_by"]}, request_token=True, request_id=rid)
        except (BadRequest, Conflict) as e:
            self.db.ex("DELETE FROM friends WHERE address=? AND state='requested_out'", (address,))
            self.db.ex("UPDATE friend_requests SET state='failed', error=?, decided_at=? WHERE id=?", (getattr(e, "detail", "")[:300], self.db.now(), rid))
            self._changed(address, deleted=True)
            self._req_changed(rid)
            raise
        self.db.ex("UPDATE friend_requests SET envelope_id=? WHERE id=?", (r["id"], rid))

    async def approve(self, rid: int) -> s.FriendRequest:
        req = self._request_row(rid)
        if req["state"] != "pending":
            raise Conflict("already_decided", f"That request is {req['state']}.")
        self._need_joined()
        if req["direction"] == "out":  # the assistant's draft: now the owner has approved it
            await self._send_request(rid)
            return self.request_model(self._request_row(rid))
        address = req["address"]
        f = self._friend_row(address)
        if f and f["state"] == "blocked":
            raise Conflict("blocked", f"You blocked {address}.")
        token = json.loads(req["token_json"]) if req["token_json"] else None
        pin = await self._pin(address, req["fingerprint"], token)
        now = self.db.now()
        prior = f["state"] if f else None
        if f:
            self.db.ex("UPDATE friends SET state='friends', fingerprint=?, updated_at=? WHERE address=?", (pin["fingerprint"], now, address))
        else:
            self.db.ex("INSERT INTO friends(address, display_name, fingerprint, state, created_at, updated_at) VALUES(?,?,?,'friends',?,?)",
                       (address, address, pin["fingerprint"], now, now))
        self._thread_for(address)
        self.db.ex("UPDATE friend_requests SET state='approved', decided_at=? WHERE id=?", (now, rid))
        for crossing in self.db.q("SELECT id FROM friend_requests WHERE direction='out' AND address=? AND state='sent'", (address,)):  # we asked each other
            self.db.ex("UPDATE friend_requests SET state='approved', decided_at=? WHERE id=?", (now, crossing["id"]))
            self._req_changed(crossing["id"])
        self._changed(address)
        self._req_changed(rid)
        try:
            await self._send(address, "friend-accept", {"request": req["envelope_id"]}, request_token=True, request_id=rid)
        except (BadRequest, Conflict):
            self.db.ex("UPDATE friend_requests SET state='pending', decided_at=NULL WHERE id=?", (rid,))
            if prior:
                self.db.ex("UPDATE friends SET state=? WHERE address=?", (prior, address))
            self._changed(address)
            self._req_changed(rid)
            raise
        return self.request_model(self._request_row(rid))

    def decline(self, rid: int) -> s.FriendRequest:
        req = self._request_row(rid)
        if req["state"] != "pending":
            raise Conflict("already_decided", f"That request is {req['state']}.")
        self.db.ex("UPDATE friend_requests SET state='declined', decided_at=?, code=NULL WHERE id=?", (self.db.now(), rid))
        if req["direction"] == "in" and not self.db.q1("SELECT 1 FROM friend_requests WHERE direction='in' AND address=? AND state='pending'", (req["address"],)):
            if self.db.ex("DELETE FROM friends WHERE address=? AND state='requested_in'", (req["address"],)):  # saying nothing: they simply never hear back
                self._changed(req["address"], deleted=True)
        self._req_changed(rid)
        return self.request_model(self._request_row(rid))

    # ── ending it ──────────────────────────────────────────────────────────
    async def _drop_pin(self, address: str) -> None:
        for path in ("/v0/peer/revoke", "/v0/peer/forget"):
            try:
                await self._g("POST", path, {"address": address})
            except (BadRequest, Conflict) as e:
                log.info("%s for %s: %s", path, address, getattr(e, "detail", e))  # not pinned, or the gateway is away: nothing more to undo

    async def unfriend(self, address: str) -> None:
        """Tell them, revoke our token, forget their key. The peer thread stays (it's history)."""
        address = address.lower()
        f = self._friend_row(address)
        if not f:
            raise NotFound(f"friend {address}")
        if f["state"] == "blocked":
            raise Conflict("blocked", "They're blocked: use unblock.")
        if f["state"] == "friends" and self.m.joined() and not self.paused():
            try:
                await self._send(address, "unfriend", {})  # first: queued entries keep their host, so it still delivers after the forget
            except (BadRequest, Conflict) as e:
                log.info("couldn't send unfriend to %s: %s", address, getattr(e, "detail", e))
        await self._drop_pin(address)
        for r in self.db.q("SELECT id FROM friend_requests WHERE address=? AND state IN ('pending','sent')", (address,)):
            self.db.ex("UPDATE friend_requests SET state=?, decided_at=?, code=NULL WHERE id=?", ("declined" if f["state"] != "friends" else "approved", self.db.now(), r["id"]))
            self._req_changed(r["id"])
        self.db.ex("DELETE FROM friends WHERE address=?", (address,))
        if f["thread_id"]:
            self.conv.system_message(f["thread_id"], f"You're no longer friends with {f['display_name']}.")
        self._changed(address, deleted=True)

    async def block(self, address: str) -> s.Friend:
        address = self._address(address)
        f = self._friend_row(address)
        now = self.db.now()
        await self._drop_pin(address)
        if f:
            self.db.ex("UPDATE friends SET state='blocked', updated_at=? WHERE address=?", (now, address))
        else:
            self.db.ex("INSERT INTO friends(address, display_name, state, created_at, updated_at) VALUES(?,?,'blocked',?,?)", (address, address, now, now))
        for r in self.db.q("SELECT id FROM friend_requests WHERE address=? AND state IN ('pending','sent')", (address,)):
            self.db.ex("UPDATE friend_requests SET state='declined', decided_at=?, code=NULL WHERE id=?", (now, r["id"]))
            self._req_changed(r["id"])
        self._changed(address)
        return self.friend(address)

    def unblock(self, address: str) -> None:
        address = address.lower()
        f = self._friend_row(address)
        if not f or f["state"] != "blocked":
            raise NotFound(f"blocked friend {address}")
        self.db.ex("DELETE FROM friends WHERE address=?", (address,))
        self._changed(address, deleted=True)

    # ── our messages to a friend ───────────────────────────────────────────
    def queue_message(self, thread_id: int, message_id: int) -> int | None:
        """A message the owner wrote in a peer thread: remember it for sending. None if the thread isn't a peer thread."""
        t = self.db.q1("SELECT type FROM threads WHERE id=?", (thread_id,))
        if not t or t["type"] != "peer":
            return None
        f = self.db.q1("SELECT address FROM friends WHERE thread_id=?", (thread_id,))
        if f is None:  # the friendship ended; the thread is history
            self.conv.system_message(thread_id, "You're not friends any more, so this wasn't sent.")
            return None
        now = self.db.now()
        return self.db.ex("INSERT INTO peer_outbox(to_address, kind, message_id, created_at, updated_at) VALUES(?,'message',?,?,?)", (f["address"], message_id, now, now))

    async def flush(self) -> None:
        """Send messages waiting for the gateway (just written, or held while paused or while the gateway was away)."""
        if self.paused() or not self.m.joined() or not self.m.gw:
            return
        for ob in self.db.q("SELECT * FROM peer_outbox WHERE kind='message' AND state='pending' AND envelope_id IS NULL ORDER BY id"):
            if ob["id"] in self._inflight:
                continue
            if parse(ob["created_at"]) < self.now() - timedelta(hours=self.settings.get("PEER_PENDING_GIVE_UP_HOURS")):  # type: ignore[operator]
                self._outcome({**ob, "state": "queued"}, "failed", "the gateway couldn't be reached in time")
                continue
            m = self.db.q1("SELECT * FROM messages WHERE id=?", (ob["message_id"],))
            f = self._friend_row(ob["to_address"])
            if not m or not f or f["state"] != "friends":
                self._outcome({**ob, "state": "queued"}, "failed", "you're not friends")
                continue
            self._inflight.add(ob["id"])
            try:
                try:
                    files = await asyncio.to_thread(self._load_files, m["id"])
                except ValueError as e:  # a file went missing, or grew past the limit, since the message was written
                    self._outcome({**ob, "state": "queued"}, "failed", str(e))
                    continue
                body = {"thread": str(m["thread_id"]), "reply_to": None, "text": self.r(m["body"])[:TEXT_MAX], "files": files}
                await self._send(ob["to_address"], "message", body, message_id=ob["message_id"], outbox_id=ob["id"])
            except (BadRequest, Conflict) as e:
                if self._transient(e) or (isinstance(e, Conflict) and e.error == "paused"):
                    continue  # still pending: tried again on the next tick
                self._outcome({**ob, "state": "queued"}, "failed", getattr(e, "detail", "") or e.error)
            finally:
                self._inflight.discard(ob["id"])

    def _load_files(self, message_id: int) -> list[dict]:
        """A message's attachments as the spec's `files` (name, type, size, sha256, data), read through Storage (no symlink is followed,
        only regular files) or from an upload's place in /work. Raises ValueError, saying why, when one can't go or they total over 5 MiB."""
        out, total = [], 0
        for a in self.db.q("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (message_id,)):
            if self.storage is None:
                raise ValueError("control can't read the files here")
            try:
                data = self.storage.read(a["volume"], a["path"], limit=max(0, MAX_FILES - total))
            except BadRequest as e:
                if e.error == "too_large":
                    raise ValueError(f"the files total more than {MAX_FILES // 1024 // 1024} MiB") from None
                raise ValueError(f"{a['name']} can't be sent: {e.detail or e.error}") from None
            except NotFound:
                raise ValueError(f"{a['name']} isn't there any more") from None
            total += len(data)
            mime = (a["mime"] or "").lower()
            out.append(build_file(safe_file_name(a["name"]), mime if MEDIA_TYPE.match(mime) else "application/octet-stream", data))
        return out

    async def send_presence(self, address: str, body: dict) -> None:
        """One presence envelope to a friend (presence.py decides what and when). Raises when the gateway refuses it."""
        await self._send(address, "presence", body)

    # ── housekeeping: outbox states, expiry, key checks ────────────────────
    async def housekeeping(self, now: datetime) -> None:
        self.breaker.housekeeping(now)
        if not self.m.joined() or not self.m.gw:
            return
        try:
            await self.flush()
            await self._wake_housekeeping(now)
            await self._poll_outbox()
            if self._last_slow is None or now - self._last_slow >= timedelta(hours=1):
                self._last_slow = now
                self._expire(now)
                await self._check_keys(now)
        except Exception:  # noqa: BLE001  (the other housekeeping must still run)
            log.exception("friends housekeeping")

    async def _poll_outbox(self) -> None:
        if not self.db.q1("SELECT 1 FROM peer_outbox WHERE state='queued' AND envelope_id IS NOT NULL LIMIT 1"):
            return
        row = self.db.q1("SELECT value FROM kv WHERE key='peer_outbox_since'")
        path = "/v0/peer/outbox" + (f"?since={quote(row['value'])}" if row else "")
        try:
            entries = (await self.m.gw.call("GET", path)).get("entries", [])  # type: ignore[union-attr]
        except GatewayError as e:
            log.info("peer outbox not read: %s", e)
            return
        newest = row["value"] if row else ""
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get("id"), str):
                continue
            newest = max(newest, str(e.get("updated") or ""))
            ob = self.db.q1("SELECT * FROM peer_outbox WHERE envelope_id=?", (e["id"],))
            if ob:
                self._outcome(ob, str(e.get("state")), e.get("error"))
        if newest:
            self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('peer_outbox_since', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                       (newest, self.db.now()))

    def _expire(self, now: datetime) -> None:
        old = iso(now - timedelta(days=REQUEST_EXPIRE_DAYS))
        for r in self.db.q("SELECT * FROM friend_requests WHERE state='pending' AND created_at<?", (old,)):
            self.db.ex("UPDATE friend_requests SET state='expired', decided_at=?, code=NULL WHERE id=?", (self.db.now(), r["id"]))
            if r["direction"] == "in" and self.db.ex("DELETE FROM friends WHERE address=? AND state='requested_in'", (r["address"],)):
                self._changed(r["address"], deleted=True)
            self._req_changed(r["id"])
        self.db.ex("DELETE FROM peer_envelopes WHERE received_at<?", (iso(now - timedelta(days=DEDUP_DAYS)),))
        self.db.ex("DELETE FROM peer_wakes WHERE created_at<? AND (outcome IS NOT NULL OR direction='out')", (iso(now - timedelta(days=30)),))
        self.db.ex("DELETE FROM peer_outbox WHERE state IN ('delivered','failed') AND updated_at<?", (iso(now - timedelta(days=30)),))

    async def _check_keys(self, now: datetime) -> None:
        """Daily: look their directory entries up again. A key that changed without a rotation raises the alarm (never accepted by itself)."""
        if self.paused():
            return
        cut = iso(now - KEY_CHECK_EVERY)
        for f in self.db.q("SELECT * FROM friends WHERE state='friends' AND (checked_at IS NULL OR checked_at<?)", (cut,)):
            try:
                pin = await self._pin(f["address"])
            except (BadRequest, Conflict) as e:
                self.db.ex("UPDATE friends SET checked_at=? WHERE address=?", (self.db.now(), f["address"]))
                log.info("key check for %s: %s", f["address"], getattr(e, "detail", e))
                continue
            self.db.ex("UPDATE friends SET checked_at=?, fingerprint=?, key_alarm=NULL, updated_at=? WHERE address=?", (self.db.now(), pin["fingerprint"], self.db.now(), f["address"]))
            if pin["fingerprint"] != f["fingerprint"] or f["key_alarm"]:
                self._changed(f["address"])

    # ── incoming ───────────────────────────────────────────────────────────
    async def receive(self, raw: bytes) -> tuple[int, dict, dict[str, str]]:
        """What `POST /api/peer/v0/inbox` does. -> (status, body, headers)."""
        try:
            return await self._receive(raw)
        except Reject as e:
            headers = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
            return e.status, _error_body(e.error, e.detail), headers

    async def _receive(self, raw: bytes) -> tuple[int, dict, dict[str, str]]:
        if self.paused():
            raise Reject(503, "paused", "This instance is not taking messages right now.", 300)
        if (wait := self.overall.hit("*")) is not None:
            raise Reject(429, "rate_limited", "Too many envelopes; try again later.", wait)
        if not self.m.joined() or not self.m.gw:
            raise Reject(404, "not_here", "")
        try:
            opened = await self.m.gw.call("POST", "/v0/peer/open", raw=raw)
        except GatewayError as e:
            if e.error in OPEN_ERRORS and e.status != 0:
                raise Reject(OPEN_ERRORS[e.error], e.error, "" if e.error == "bad_signature" else e.detail, 30 if e.error == "unresolvable" else None) from e
            log.warning("the gateway couldn't open an envelope: %s", e)
            raise Reject(503, "unavailable", "Try again in a moment.", 30) from e
        sender, kind, env_id = str(opened.get("from", "")).lower(), opened.get("kind"), opened.get("id")
        if not sender or kind not in PEER_KINDS + SERVICE_KINDS or not isinstance(env_id, str):
            raise Reject(400, "bad_document", "")
        if (wait := self.per_sender.hit(sender)) is not None:
            raise Reject(429, "rate_limited", "Too many envelopes from you; slow down.", wait)
        if self.db.q1("SELECT 1 FROM peer_envelopes WHERE sender=? AND envelope_id=?", (sender, env_id)):
            return 200, {"v": "l0w/0.1", "duplicate": True}, {}
        if kind == "friend-request":
            day = iso(self.now() - timedelta(days=1))
            if self.db.q1("SELECT COUNT(*) AS n FROM peer_envelopes WHERE kind='friend-request' AND sender=? AND received_at>=?", (sender, day))["n"] >= REQUESTS_PER_DAY_SENDER \
                    or self.db.q1("SELECT COUNT(*) AS n FROM peer_envelopes WHERE kind='friend-request' AND received_at>=?", (day,))["n"] >= REQUESTS_PER_DAY_OVERALL:
                raise Reject(429, "rate_limited", "Too many friend requests.", 3600)
        after: list[Callable] = []
        with self.db.tx() as db:  # the envelope's id and what it did are stored together, before the 202
            try:
                db.ex("INSERT INTO peer_envelopes(sender, envelope_id, kind, received_at) VALUES(?,?,?,?)", (sender, env_id, kind, db.now()))
            except sqlite3.IntegrityError:
                return 200, {"v": "l0w/0.1", "duplicate": True}, {}
            self._store(sender, opened, after)
        for fn in after:
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001  (the envelope is stored; a failed side effect must not make the sender resend it)
                log.exception("after receiving a %s from %s", kind, sender)
        return 202, {"v": "l0w/0.1", "id": env_id}, {}

    def _store(self, sender: str, opened: dict, after: list[Callable]) -> None:
        kind, body = opened["kind"], opened.get("body") if isinstance(opened.get("body"), dict) else {}
        if kind in SERVICE_KINDS:  # a hub service, not a friend: the gateway took it only from an address control named a service
            if self.hubservices:
                self.hubservices.on_envelope(sender, opened, body, after)
            return
        f = self._friend_row(sender)
        if f and f["state"] == "blocked":
            return  # answered 202 and dropped
        if kind == "friend-request":
            self._on_request(sender, opened, body, f, after)
            return
        if not f:
            return  # not someone we know (an unfriend that crossed with ours, say): nothing to do
        handler = {"friend-accept": self._on_accept, "friend-decline": self._on_decline, "unfriend": self._on_unfriend, "message": self._on_message,
                   "presence": self._on_presence, "wake-request": self._on_wake_request, "wake-reply": self._on_wake_reply, "same-owner": self._on_same_owner}[kind]
        handler(f, opened, body, after)

    def _on_request(self, sender: str, opened: dict, body: dict, f: dict | None, after: list[Callable]) -> None:
        if f and f["state"] in ("friends", "requested_in"):
            return  # already friends (they'll find out we never saw this), or already waiting on the owner
        has_code = self._claim_code(body.get("code"), sender)
        if not has_code:
            mode = self.requests_from()
            me = self.m.joined()
            if mode == "codes" or (mode == "hub" and not (me and sender.split("@", 1)[-1] == me["hub_domain"])):
                return  # dropped silently, answered 202
        now = self.db.now()
        fp = opened.get("fingerprint")
        by = body.get("by") if body.get("by") in ("owner", "assistant") else "owner"
        token = body.get("token") if isinstance(body.get("token"), dict) else None
        for old in self.db.q("SELECT id FROM friend_requests WHERE direction='in' AND address=? AND state='pending'", (sender,)):
            self.db.ex("UPDATE friend_requests SET state='expired', decided_at=? WHERE id=?", (now, old["id"]))
            self._req_changed(old["id"])
        rid = self.db.ex("INSERT INTO friend_requests(direction, address, envelope_id, note, made_by, code_used, fingerprint, token_json, state, created_at) "
                         "VALUES('in',?,?,?,?,?,?,?,'pending',?)",
                         (sender, opened["id"], self.r(str(body.get("note") or ""))[:NOTE_MAX], by, int(has_code), fp, json.dumps(token) if token else None, now))
        if f is None:
            self.db.ex("INSERT INTO friends(address, display_name, fingerprint, state, created_at, updated_at) VALUES(?,?,?,'requested_in',?,?)", (sender, sender, fp, now, now))
        self._changed(sender)
        self._req_changed(rid)
        after.append(lambda: self.notifier.notify("friends", "Friend request", f"{sender} would like to be friends.", "/friends", "friend_request", str(rid)))

    def _on_accept(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "requested_out":
            return
        now = self.db.now()
        self.db.ex("UPDATE friends SET state='friends', fingerprint=COALESCE(?, fingerprint), updated_at=? WHERE address=?", (opened.get("fingerprint"), now, f["address"]))
        self._thread_for(f["address"])
        for r in self.db.q("SELECT id FROM friend_requests WHERE direction='out' AND address=? AND state='sent'", (f["address"],)):
            self.db.ex("UPDATE friend_requests SET state='approved', decided_at=? WHERE id=?", (now, r["id"]))
            self._req_changed(r["id"])
        self._changed(f["address"])
        after.append(lambda: self.notifier.notify("friends", "Friend request accepted", f"{f['display_name']} accepted. You're friends.", "/friends", "friend", f["address"]))

    def _on_decline(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "requested_out":
            return
        now = self.db.now()
        for r in self.db.q("SELECT id FROM friend_requests WHERE direction='out' AND address=? AND state='sent'", (f["address"],)):
            self.db.ex("UPDATE friend_requests SET state='declined', decided_at=? WHERE id=?", (now, r["id"]))
            self._req_changed(r["id"])
        self.db.ex("DELETE FROM friends WHERE address=?", (f["address"],))
        self._changed(f["address"], deleted=True)
        after.append(lambda: self._drop_pin(f["address"]))
        after.append(lambda: self.notifier.notify("friends", "Friend request declined", f"{f['address']} declined.", "/friends", "friend", f["address"]))

    def _on_unfriend(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] not in ("friends", "requested_out", "requested_in"):
            return
        addr, name = f["address"], f["display_name"]
        for r in self.db.q("SELECT id, direction FROM friend_requests WHERE address=? AND state IN ('pending','sent')", (addr,)):
            self.db.ex("UPDATE friend_requests SET state='declined', decided_at=?, code=NULL WHERE id=?", (self.db.now(), r["id"]))
            self._req_changed(r["id"])
        self.db.ex("DELETE FROM friends WHERE address=?", (addr,))
        if f["thread_id"]:
            self.conv.system_message(f["thread_id"], f"{name} ended the friendship.")
        self._changed(addr, deleted=True)
        after.append(lambda: self._drop_pin(addr))
        if f["state"] == "friends":
            after.append(lambda: self.notifier.notify("friends", "Friendship ended", f"{name} unfriended you.", "/friends", "friend", addr))

    def _on_message(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "friends":
            return
        tid = self._thread_for(f["address"])
        text = self.r(str(body.get("text") or ""))
        notes: list[str] = []
        stored: list[tuple[str, str, int, str]] = []
        for item in body.get("files") or []:
            try:
                stored.append(self._store_file(f["address"], item))
            except (ValueError, TypeError, KeyError) as e:
                name = safe_file_name(item.get("name") if isinstance(item, dict) else None)
                notes.append(f"(a file, {name}, was left out: {e})")
        if len(text) > TEXT_MAX:
            text = text[:TEXT_MAX] + "\n(cut: too long)"
        if notes:
            text = (text + "\n\n" if text else "") + "\n".join(notes)
        thread = self.db.q1("SELECT status, title FROM threads WHERE id=?", (tid,))
        if thread and thread["status"] == "archived":
            self.conv.patch_thread(tid, {"status": "open"})
        mid = self.db.ex("INSERT INTO messages(thread_id, sender, body, via, created_at) VALUES(?,?,?,?,?)", (tid, "peer", text, "peer", self.db.now()))
        for name, path, size, mime in stored:
            self.db.ex("INSERT INTO attachments(message_id, upload_id, name, volume, path, size, mime) VALUES(?,NULL,?,'work',?,?,?)", (mid, name, path, size, mime))
        self.db.ex("UPDATE peer_envelopes SET message_id=? WHERE sender=? AND envelope_id=?", (mid, f["address"], opened["id"]))
        self.live.changed("message", mid)
        self.conv._touch_thread(tid)
        preview = self.conv._preview(text, 120)
        after.append(lambda: self._notify(f["address"], f"{f['display_name']}'s assistant wrote", preview, f"/t/{tid}", "message"))

    def _store_file(self, address: str, item: object) -> tuple[str, str, int, str]:
        """One file of a message, into _inbox/peers/<address>/. -> (name, path under /work, size, media type). Raises ValueError."""
        if not isinstance(item, dict):
            raise ValueError("not a file")
        name = safe_file_name(item.get("name"))
        try:
            data = b64.decode(str(item.get("data")))
        except Exception as e:  # noqa: BLE001
            raise ValueError("couldn't decode it") from e
        if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != str(item.get("sha256", "")).lower():
            raise ValueError("its size or hash doesn't match")
        sub = f"peers/{folder_name(address)}"
        try:
            stored = self.write_inbox(sub, name, data)
        except (BadRequest, Conflict) as e:
            raise ValueError(getattr(e, "detail", "") or "couldn't save it") from e
        mime = str(item.get("type") or "").lower()
        return stored, f"_inbox/{sub}/{stored}", len(data), mime if MEDIA_TYPE.match(mime) else "application/octet-stream"

    def _on_presence(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "friends":
            return
        iat = str(opened.get("iat") or "")
        try:
            at = iso(parse(iat))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            at = self.db.now()
        if f["presence_at"] and at <= f["presence_at"]:
            return  # a retry that was overtaken
        p = {"state": body.get("state"), "next": body.get("next"), "now": self.r(body["now"])[:200] if isinstance(body.get("now"), str) else None}
        self.db.ex("UPDATE friends SET presence_json=?, presence_at=?, updated_at=? WHERE address=?", (json.dumps(p), at, self.db.now(), f["address"]))
        self._changed(f["address"])

    # ── waking ─────────────────────────────────────────────────────────────
    def _on_wake_request(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        """A friend's assistant asks to wake ours. Stored here, decided right after (never inside the receiving transaction)."""
        if f["state"] != "friends":
            return
        reason = self.r(str(body.get("reason") or ""))[:500]
        pw = self.db.ex("INSERT INTO peer_wakes(direction, address, envelope_id, reason, created_at) VALUES('in',?,?,?,?)", (f["address"], opened["id"], reason, self.db.now()))
        after.append(lambda: self._decide_wake(pw))

    async def _decide_wake(self, pw: int) -> None:
        w = self.db.q1("SELECT * FROM peer_wakes WHERE id=? AND direction='in'", (pw,))
        f = self._friend_row(w["address"]) if w else None
        if not w or w["outcome"] or w["permission_id"]:
            return
        if not f or f["state"] != "friends":
            self.db.ex("UPDATE peer_wakes SET outcome='denied', replied_at=? WHERE id=?", (self.db.now(), pw))
            return
        addr, name, tid = f["address"], f["display_name"], self._thread_for(f["address"])
        said = f"{name}'s assistant asked to wake yours: “{w['reason']}”."
        mode = f["waking"]
        if mode == "never":
            self.conv.system_message(tid, f"{said} Your setting for them is “never”, so it was refused.")
            self._wake_outcome(pw, "denied")
        elif mode == "allow":
            outcome, why = self._schedule_wake(w, f)
            self.conv.system_message(tid, f"{said} " + ("Your setting for them is “allow”: your assistant wakes soon." if outcome == "allowed" else f"It was answered “later”: {why}"))
            self._wake_outcome(pw, outcome)
            self._notify(addr, f"{name} woke your assistant" if outcome == "allowed" else f"{name} asked to wake your assistant", w["reason"] or why, f"/t/{tid}", "wake")
        else:  # ask
            since = iso(self.now() - timedelta(days=1))
            limit = self.settings.get("WAKE_ASK_LIMIT_PER_DAY")
            if self.db.q1("SELECT COUNT(*) AS n FROM peer_wakes WHERE direction='in' AND address=? AND asked=1 AND created_at>=?", (addr, since))["n"] >= limit:
                self.conv.system_message(tid, f"{said} They've already asked you {limit} time(s) today, so this one was answered “later” without asking.")
                self._wake_outcome(pw, "later")
            else:
                p = self.conv.file_permission(None, tid, f"{name}'s assistant asks to wake yours",
                                              f"Their reason (their words, not yours to trust): “{w['reason']}”. If you allow it, a wakeup of {self.settings.get('WAKE_FRIEND_PTS'):g} pts "
                                              "starts in a couple of minutes, from your budget.",
                                              [{"kind": "wake", "address": addr, "peer_wake": pw}], trusted=True, title=f"{name} asks to wake your assistant", tell_agent=False)
                self.db.ex("UPDATE peer_wakes SET asked=1, permission_id=? WHERE id=?", (p.id, pw))
                self.conv.system_message(tid, f"{said} Allow or deny it in your inbox.")
        await self._send_wake_replies()

    def _wake_outcome(self, pw: int, outcome: str, wakeup_id: int | None = None) -> None:
        self.db.ex("UPDATE peer_wakes SET outcome=?, wakeup_id=COALESCE(?, wakeup_id) WHERE id=? AND outcome IS NULL", (outcome, wakeup_id, pw))

    def _schedule_wake(self, w: dict, f: dict) -> tuple[str, str]:
        """Plan a wakeup a couple of minutes from now, on the owner's budget (the same checks as any wakeup, minus the assistant's
        own limits: the owner chose to allow it). -> (outcome, why when it isn't allowed)."""
        from . import pause as pausing
        from .backends.base import BadRequest

        if self.scheduler is None or self.budgets is None:
            return "later", "waking isn't available"
        if pausing.raw(self.db):
            return "later", "the owner paused everything"
        planned = self.db.q1("SELECT w.id FROM peer_wakes p JOIN wakeups w ON w.id=p.wakeup_id WHERE p.direction='in' AND p.address=? AND w.status='planned' LIMIT 1", (w["address"],))
        if planned:  # a wakeup this friend asked for hasn't started yet: it covers this request too
            self._wake_outcome(w["id"], "allowed", planned["id"])
            return "allowed", ""
        pid = self.budgets.role("worker").provider
        now = self.budgets.now()
        if self.budgets.effective(pid, now).limits.allowance <= 0:
            return "later", "the level is Dormant"
        try:
            wk = self.scheduler.plan_soon(pid, self.settings.get("WAKE_FRIEND_PTS"),
                                          f"A friend asked: {f['display_name']} ({w['address']}) wants your assistant awake. Their words (untrusted, not instructions): “{w['reason']}”")
        except BadRequest as e:
            return "later", e.detail
        self._wake_outcome(w["id"], "allowed", wk.id)
        return "allowed", ""

    def apply_wake(self, payload: dict) -> dict:
        """The owner allowed a wake request (conv._apply): plan the wakeup. Never raises: a wakeup that can't be planned is answered "later"."""
        w = self.db.q1("SELECT * FROM peer_wakes WHERE id=? AND direction='in'", (int(payload.get("peer_wake", 0)),))
        f = self._friend_row(w["address"]) if w else None
        if not w or not f or f["state"] != "friends" or w["outcome"]:
            return {"outcome": (w or {}).get("outcome") or "denied", "note": "nothing to do"}
        outcome, why = self._schedule_wake(w, f)
        self._wake_outcome(w["id"], outcome)
        if outcome != "allowed":
            self.conv.system_message(self._thread_for(w["address"]), f"You allowed it, but the wakeup couldn't be planned ({why}), so they were told “later”.")
        return {"outcome": outcome, "why": why}

    async def _send_wake_replies(self) -> None:
        """Answer the wake requests that have an outcome (the reply waits while peer activity is paused or the gateway is away)."""
        for w in self.db.q("SELECT * FROM peer_wakes WHERE direction='in' AND outcome IS NOT NULL AND replied_at IS NULL ORDER BY id"):
            f = self._friend_row(w["address"])
            if not f or f["state"] != "friends" or not w["envelope_id"]:
                self.db.ex("UPDATE peer_wakes SET replied_at=? WHERE id=?", (self.db.now(), w["id"]))
                continue
            try:
                await self._send(w["address"], "wake-reply", {"request": w["envelope_id"], "outcome": w["outcome"]})
            except (BadRequest, Conflict) as e:
                if self._transient(e) or (isinstance(e, Conflict) and e.error == "paused"):
                    return  # tried again on the next tick
                log.info("wake reply to %s: %s", w["address"], getattr(e, "detail", e))
            self.db.ex("UPDATE peer_wakes SET replied_at=? WHERE id=?", (self.db.now(), w["id"]))

    async def _wake_housekeeping(self, now: datetime) -> None:
        """Owner decisions on asked wake requests (deny, expiry) become outcomes; then the replies go out."""
        for w in self.db.q("SELECT p.*, pm.status AS pstatus FROM peer_wakes p JOIN permissions pm ON pm.id=p.permission_id WHERE p.direction='in' AND p.outcome IS NULL"):
            if w["pstatus"] in ("denied", "withdrawn"):
                self._wake_outcome(w["id"], "denied")
            elif w["pstatus"] == "allowed":  # applied by control; this only happens if the apply was interrupted
                self._wake_outcome(w["id"], "later")
            elif w["created_at"] < iso(now - timedelta(hours=self.settings.get("WAKE_ASK_EXPIRE_HOURS"))):
                self.db.ex("UPDATE permissions SET status='withdrawn', decided_at=? WHERE id=? AND status='open'", (self.db.now(), w["permission_id"]))
                self.live.changed("permission", w["permission_id"])
                self.live.emit("inbox_counts")
                self._wake_outcome(w["id"], "later")
        for w in self.db.q("SELECT id FROM peer_wakes WHERE direction='in' AND outcome IS NULL AND permission_id IS NULL AND created_at<?", (iso(now - timedelta(minutes=2)),)):
            await self._decide_wake(w["id"])  # a decision that a restart interrupted
        await self._send_wake_replies()

    async def send_wake(self, address: str, reason: str, run_id: int | None = None) -> dict:
        """The assistant asks a friend's assistant to wake. Limited per friend per day; the answer comes back as a note to the agent."""
        address = address.lower()
        f = self._friend_row(address)
        if not f or f["state"] != "friends":
            raise Conflict("not_friends", f"{address} isn't one of your friends.")
        if self.paused():
            raise Conflict("paused", "The owner paused all peer activity (Settings → Friends), so nothing was sent.")
        self.breaker.gate(run_id, f)
        limit = self.settings.get("WAKE_SEND_LIMIT_PER_DAY")
        if self.db.q1("SELECT COUNT(*) AS n FROM peer_wakes WHERE direction='out' AND address=? AND created_at>=?", (address, iso(self.now() - timedelta(days=1))))["n"] >= limit:
            raise Conflict("limit", f"You've already asked {f['display_name']}'s assistant to wake {limit} time(s) in the last day. Message them instead, or wait.")
        reason = self.r(reason.strip())[:500]
        r = await self._send(address, "wake-request", {"reason": reason})
        self.db.ex("INSERT INTO peer_wakes(direction, address, envelope_id, reason, created_at) VALUES('out',?,?,?,?)", (address, r["id"], reason, self.db.now()))
        self.conv.system_message(self._thread_for(address), f"Your assistant asked {f['display_name']}'s assistant to wake: “{reason}”.")
        return {"address": address, "id": r["id"]}

    def _on_wake_reply(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "friends":
            return
        tid = self._thread_for(f["address"])
        outcome = body.get("outcome") if body.get("outcome") in ("allowed", "denied", "later") else "unknown"
        self.conv.system_message(tid, f"{f['display_name']}'s assistant answered the wake request: {outcome}.")
        mine = self.db.q1("SELECT id FROM peer_wakes WHERE direction='out' AND address=? AND envelope_id=? AND outcome IS NULL", (f["address"], str(body.get("request") or "")))
        if mine and outcome != "unknown":  # only an answer to a request we made
            self.db.ex("UPDATE peer_wakes SET outcome=? WHERE id=?", (outcome, mine["id"]))
            self.tell_agent(f"{f['display_name']}'s assistant answered your wake request: {outcome}.")
        after.append(lambda: self._notify(f["address"], f"{f['display_name']} answered your wake request", outcome, f"/t/{tid}", "wake"))

    def _on_same_owner(self, f: dict, opened: dict, body: dict, after: list[Callable]) -> None:
        if f["state"] != "friends" or not isinstance(body.get("statement"), dict):
            return
        self.db.ex("UPDATE friends SET same_owner_json=?, updated_at=? WHERE address=?", (json.dumps(body["statement"]), self.db.now(), f["address"]))
        self._changed(f["address"])
        after.append(lambda: self._notify(f["address"], f"{f['display_name']} says you're the same owner",
                                          "A hint for your defaults (like waking); nothing changes on its own.", "/friends", "friend"))
