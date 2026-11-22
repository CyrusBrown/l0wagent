"""A hub's services, instance side (H6). The spec is protocol/services.md.

The hub lists its services in its capabilities, each a signed `service` document. Control reads them (l0wproto's
`services_in`) on joining, on every capabilities refresh and now and then in housekeeping, keeps one `hub_services` row
per service, and connects the ones the owner has on: pin the service's keys at the gateway, hand it a delivery token with a
tokenless `service-hello`, and take its token and role grants from the `service-welcome`.

Who holds what: the gateway holds keys, tokens and the outbox (and takes `service-*` envelopes only from the addresses
control names, `PUT /v0/peer/services`); control holds the owner's switch, the connection's state, the **role grants** and
the call log. Role grants never leave control: a caller (the agent, an extension, the owner) may name a role, and control
attaches the grant it holds to the `service-call`.

Calls (`Hubservices.call`) go out as `service-call` envelopes and wait for the matching `service-reply` (an asyncio
future keyed by the call id, plus the `service_calls` row, so a restart marks what was pending as timed out). Everything
a service sends (results, events, even its title) is untrusted content: results and events are stored redacted, and the
agent is told so wherever it sees them. A service can't authorize anything for the assistant: its answers are data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from l0wproto import payloads as P
from l0wproto.errors import ProtocolError
from l0wproto.keys import Key
from l0wproto.times import parse_time

from . import __version__
from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import Conflict, NotFound
from .friends import Window
from .membership import GatewayError

log = logging.getLogger("l0w.hubservices")

CALL_TIMEOUT = 300.0  # the spec's wait for a reply
CONNECT_WAIT = timedelta(minutes=10)  # a hello with no welcome after this is an error
RETRY_AFTER_ERROR = timedelta(minutes=30)
RESYNC_EVERY = timedelta(hours=1)
KEEP_CALLS = timedelta(days=30)
NEW_SERVICE_NOTICE_AFTER = timedelta(minutes=10)  # a service that shows up this long after joining is announced
EVENT_NOTES_PER_HOUR = 10
AGENT_CALLS_PER_MINUTE = 60
BY_RE = ("agent", "owner")
TEXT_MAX = 2000


class ServiceError(Exception):
    """A call that didn't give a result. `code` is the service's own error code (`not_allowed`, `unknown_method`, ...) when it
    answered, else one of control's: `unknown_service`, `not_connected`, `no_role`, `bad_args`, `too_large`, `paused`,
    `unavailable`, `refused`, `timeout`. `from_service` says which."""

    def __init__(self, code: str, detail: str = "", *, from_service: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.from_service = code, detail, from_service


def _clean(text: object, limit: int = TEXT_MAX) -> str:
    """Text from the hub or a service, for storage: printable, one length."""
    return "".join(c for c in str(text or "") if c.isprintable() or c in "\n\t")[:limit]


class Hubservices:
    def __init__(self, db, live, vault, settings, membership, notifier, now: Callable[[], datetime] = utcnow):
        self.db, self.live, self.vault, self.settings, self.m, self.notifier, self.now = db, live, vault, settings, membership, notifier, now
        self.event_sink: Callable[[dict, str, dict], int] | None = None  # set by the backend: hands an event to the extensions
        self._lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future] = {}
        self._pushed: list[str] | None = None  # the service addresses last given to the gateway
        self._last_sync: datetime | None = None
        self._event_notes = Window(EVENT_NOTES_PER_HOUR, 3600)
        self.agent_calls = Window(AGENT_CALLS_PER_MINUTE, 60)  # per service: the agent's runaway loop is the service's `rate_limited` last, ours first
        membership.on_caps = self.sync
        membership.on_left = self.forget_all

    # ── small helpers ──────────────────────────────────────────────────────
    def _redact(self, value):
        """Redact every string in a JSON value (keys too)."""
        if isinstance(value, str):
            return self.vault.redact(value) or ""
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, dict):
            return {self._redact(str(k)): self._redact(v) for k, v in value.items()}
        return value

    def _changed(self) -> None:
        self.live.emit("membership")

    def paused(self) -> bool:
        return bool(self.settings.get("PEER_PAUSED"))

    def _row(self, address: str) -> dict | None:
        return self.db.q1("SELECT * FROM hub_services WHERE address=? AND present=1", (address.lower(),))

    def _rows(self) -> list[dict]:
        return self.db.q("SELECT * FROM hub_services WHERE present=1 ORDER BY title COLLATE NOCASE, address")

    def _set(self, address: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.ex(f"UPDATE hub_services SET {cols}, updated_at=? WHERE address=?", (*fields.values(), self.db.now(), address))

    @staticmethod
    def _grants(row: dict) -> list[dict]:
        try:
            g = json.loads(row["grants_json"] or "[]")
        except ValueError:
            return []
        return g if isinstance(g, list) else []

    def _usable_grants(self, row: dict) -> list[dict]:
        """The grants held and still in date."""
        now = self.now().timestamp()
        out = []
        for g in self._grants(row):
            try:
                if g.get("exp") and parse_time(g["exp"]) <= now:
                    continue
            except ProtocolError:
                continue
            out.append(g)
        return out

    def roles_held(self, row: dict) -> list[str]:
        return sorted({g["role"] for g in self._usable_grants(row)})

    def _key(self, row: dict) -> Key | None:
        try:
            return Key.from_dict(json.loads(row["sign_key_json"])) if row["sign_key_json"] else None
        except (ValueError, KeyError, TypeError, ProtocolError):
            return None

    async def _g(self, method: str, path: str, body: dict | None = None) -> dict:
        gw = self.m.gw
        if gw is None:
            raise GatewayError(0, "no_gateway", "This install has no gateway.")
        return await gw.call(method, path, body)

    # ── views ──────────────────────────────────────────────────────────────
    def model(self, r: dict) -> s.HubService:
        ext = json.loads(r["extension_json"]) if r["extension_json"] else None
        return s.HubService(
            address=r["address"], kind=r["kind"], title=r["title"], description=r["description"], docs=r["docs"], roles=json.loads(r["roles_json"] or "[]"),
            roles_held=self.roles_held(r), extension=s.HubServiceExtension(**ext) if ext else None, default_on=bool(r["default_on"]), enabled=bool(r["enabled"]),
            state=r["state"], error=r["error"], connected_at=parse(r["connected_at"]), updated_at=parse(r["updated_at"]))  # type: ignore[arg-type]

    def view(self) -> list[s.HubService]:
        return [self.model(r) for r in self._rows()] if self.m.joined() else []

    def connected(self) -> list[dict]:
        """The services a caller may use now: on, and connected. {address, kind, title, description, docs, roles}: role names held, never grants."""
        if not self.m.joined():
            return []
        return [{"address": r["address"], "kind": r["kind"], "title": r["title"], "description": r["description"], "docs": r["docs"], "roles": self.roles_held(r)}
                for r in self._rows() if r["enabled"] and r["state"] == "connected"]

    def resolve(self, name: str) -> str:
        """A service named by its address, or by its name on the joined hub (`echo` for `echo@hub.example`)."""
        text = str(name or "").strip().lower()
        if not text:
            raise ServiceError("unknown_service", "Name a service (see `assistant hubnet ls`).")
        r = self.m.joined()
        if "@" not in text and r:
            text = f"{text}@{r['hub_domain']}"
        if not self._row(text):
            raise ServiceError("unknown_service", f"No service {name!r} on your hub.")
        return text

    # ── following the hub's list ───────────────────────────────────────────
    async def sync(self, *, refresh: bool = True) -> None:
        """Read the capabilities, make the rows match, tell the gateway who the services are, and connect or disconnect to match the
        owner's switches. `refresh`: look the connected services' keys up again (an alarm if one changed with no rotation)."""
        async with self._lock:
            try:
                await self._sync(refresh)
            except Exception:  # noqa: BLE001  (the check or the join that called us must go on)
                log.exception("hub services sync")

    async def _sync(self, refresh: bool) -> None:
        r = self.m.joined()
        if not r or not self.m.gw:
            return
        try:
            hub_key = Key.from_dict(r["hub_key"])
        except (ValueError, KeyError, TypeError, ProtocolError):
            return
        listed, skipped = P.services_in(r["capabilities"] or {}, hub_key)
        self._note_skipped(skipped)
        late = (parse(r["joined_at"]) or self.now()) + NEW_SERVICE_NOTICE_AFTER < self.now()
        changed = False
        seen = set()
        for p in listed:
            addr = p["address"].lower()
            seen.add(addr)
            fields = dict(kind=p["kind"], title=_clean(p["title"], 200), description=_clean(p.get("description"), 2000), docs=p.get("docs"),
                          roles_json=json.dumps(p["roles"]), extension_json=json.dumps(p["extension"]) if p.get("extension") else None,
                          default_on=1 if p["default"] == "on" else 0)
            cur = self.db.q1("SELECT * FROM hub_services WHERE address=?", (addr,))
            if cur is None:
                now = self.db.now()
                self.db.ex("INSERT INTO hub_services(address, kind, title, description, docs, roles_json, extension_json, default_on, enabled, created_at, updated_at)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (addr, *fields.values(), fields["default_on"], now, now))
                changed = True
                if late:
                    state = " and it's on." if fields["default_on"] else "; it's off until you turn it on."
                    self.notifier.notify("hub", f"New hub service: {fields['title']}", "Your hub added it" + state, "/settings/hub", "hub_service", addr)
            else:
                before = (cur["kind"], cur["title"], cur["description"], cur["docs"], cur["roles_json"], cur["extension_json"], cur["default_on"], cur["present"])
                if before != (*fields.values(), 1):
                    self._set(addr, **fields, present=1)
                    changed = True
                    if cur["roles_json"] != fields["roles_json"]:  # a role it no longer lists is no longer honored
                        keep = set(p["roles"])
                        self._set(addr, grants_json=json.dumps([g for g in self._grants(cur) if g.get("role") in keep]))
        for gone in self.db.q("SELECT * FROM hub_services WHERE present=1"):
            if gone["address"] not in seen:
                self._set(gone["address"], present=0)
                changed = True
                if gone["enabled"]:
                    self.notifier.notify("hub", f"{gone['title']} left your hub", "The hub no longer lists this service. Calls to it are off and the connection was closed.",
                                         "/settings/hub", "hub_service", gone["address"])
        for gone in self.db.q("SELECT * FROM hub_services WHERE present=0"):
            if await self._teardown(gone["address"], forget=True):
                self.db.ex("DELETE FROM hub_services WHERE address=?", (gone["address"],))
        await self._push_addresses(sorted(seen))
        if refresh:
            await self._refresh_keys()
        await self._maintain()
        self._last_sync = self.now()
        if changed:
            self._changed()

    def _note_skipped(self, skipped: list[str]) -> None:
        """Entries of the capabilities' service list that didn't verify: logged, and the owner told once for each new set."""
        digest = hashlib.sha256("\n".join(sorted(skipped)).encode()).hexdigest() if skipped else ""
        row = self.db.q1("SELECT value FROM kv WHERE key='hub_services_skipped'")
        if (row["value"] if row else "") == digest:
            return
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('hub_services_skipped', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (digest, self.db.now()))
        if skipped:
            log.warning("hub services ignored: %s", "; ".join(skipped))
            self.notifier.notify("hub", "Some of your hub's services were ignored", _clean(skipped[0], 250), "/settings/hub", "hub_services", "skipped")

    async def _push_addresses(self, addresses: list[str]) -> None:
        if addresses == self._pushed:
            return
        try:
            await self._g("PUT", "/v0/peer/services", {"addresses": addresses})
            self._pushed = addresses
        except GatewayError as e:
            log.info("couldn't tell the gateway the service addresses: %s", e)

    async def _refresh_keys(self) -> None:
        for r in self._rows():
            if r["enabled"] and r["state"] == "connected":
                try:
                    await self._pin(r["address"])
                except GatewayError as e:
                    if e.error == "key_changed":
                        self._key_alarm(r)
                    else:
                        log.info("couldn't refresh %s's keys: %s", r["address"], e)

    def _key_alarm(self, r: dict) -> None:
        text = "Its signing key changed with no rotation from the one first seen. Not trusted until you've checked with the hub's operator."
        if r["state"] != "error" or r["error"] != text:
            self._set(r["address"], state="error", error=text)
            self.notifier.notify("hub", f"{r['title']}'s key changed", text, "/settings/hub", "hub_service", r["address"])
            self._changed()

    async def _pin(self, address: str) -> dict:
        pin = await self._g("POST", "/v0/peer/pin", {"address": address})
        key = pin.get("sign_key")
        if isinstance(key, dict):
            self._set(address, sign_key_json=json.dumps(key))
        return pin

    # ── connecting ─────────────────────────────────────────────────────────
    async def _maintain(self) -> None:
        """Bring each service to what the owner's switch says; retry what's stuck."""
        now = self.now()
        for r in self._rows():
            addr, since = r["address"], parse(r["updated_at"]) or now
            if not r["enabled"]:
                if r["state"] != "off":
                    await self._teardown(addr)
            elif r["state"] == "off":
                await self._connect(addr)
            elif r["state"] == "connecting" and now - since >= CONNECT_WAIT:
                self._set(addr, state="error", error="The service didn't answer.")
                self._changed()
            elif r["state"] == "error" and now - since >= RETRY_AFTER_ERROR and not (r["error"] or "").startswith("Its signing key"):
                await self._connect(addr)

    async def _connect(self, address: str) -> None:
        r = self._row(address)
        if not r or self.paused() or not self.m.joined():
            return
        try:
            await self._pin(address)
            self._set(address, state="connecting", error=None)
            sent = await self._g("POST", "/v0/peer/send", {"to": address, "kind": "service-hello", "body": {"release": __version__}, "request_token": True})
            if sent.get("state") == "failed":
                self._set(address, state="error", error=_clean(f"The service refused us ({sent.get('error') or 'no reason given'}).", 300))
        except GatewayError as e:
            if e.error == "key_changed":
                self._key_alarm(r)
            else:
                self._set(address, state="error", error=self._gw_text(e))
        self._changed()

    @staticmethod
    def _gw_text(e: GatewayError) -> str:
        match e.error:
            case "no_such_member":
                return "The hub's directory has no entry for this service."
            case "hub_key_changed":
                return "The hub's signing key changed with no rotation."
            case "unreachable" | "hub_capabilities" | "bad_directory" | "gateway_unreachable":
                return f"Couldn't reach or verify the service just now ({e.error})."
            case _:
                return _clean(f"The gateway refused ({e.error}).", 300)

    async def _teardown(self, address: str, *, forget: bool = False) -> bool:
        """Say goodbye and revoke the token we gave. True when the connection is fully closed (False: try again later)."""
        r = self.db.q1("SELECT * FROM hub_services WHERE address=?", (address,))
        if r is None:
            return True
        try:
            if r["state"] in ("connecting", "connected"):
                try:
                    await self._g("POST", "/v0/peer/send", {"to": address, "kind": "service-bye", "body": {}})
                except GatewayError as e:  # no token of theirs yet, or they're away: the revoke below is what counts
                    log.info("goodbye to %s not sent: %s", address, e)
            await self._g("POST", "/v0/peer/revoke", {"address": address})
            if forget:
                await self._g("POST", "/v0/peer/forget", {"address": address})
        except GatewayError as e:
            log.info("couldn't close the connection to %s: %s", address, e)
            return False
        if self.db.q1("SELECT 1 FROM hub_services WHERE address=?", (address,)):
            self._set(address, state="off", error=None, connected_at=None, grants_json="[]")
            self._changed()
        return True

    async def set_enabled(self, address: str, enabled: bool) -> s.HubService:
        """The owner's switch."""
        async with self._lock:
            if not self.m.joined():
                raise Conflict("not_joined", "This instance isn't a member of a hub.")
            r = self._row(address)
            if not r:
                raise NotFound(f"hub service {address}")
            self._set(r["address"], enabled=1 if enabled else 0)
            if enabled:
                if r["state"] in ("off", "error"):
                    await self._connect(r["address"])
            else:
                await self._teardown(r["address"])
            self._changed()
            return self.model(self._row(address))  # type: ignore[arg-type]

    async def forget_all(self) -> None:
        """This instance left its hub: nothing of its services stays."""
        async with self._lock:
            self.db.ex("DELETE FROM hub_services")
            self._pushed = None
            self._fail_pending("This instance left its hub.")
            self._changed()

    # ── incoming (called by friends.receive for service-* envelopes) ───────
    def on_envelope(self, sender: str, opened: dict, body: dict, after: list[Callable]) -> None:
        """One verified `service-*` envelope, inside the receive transaction (no awaiting: later work goes in `after`)."""
        r = self._row(sender)
        if not r or not r["enabled"]:
            return  # a service we haven't switched on (or don't know) is answered 202 and ignored
        kind = opened["kind"]
        if kind == "service-welcome":
            self._on_grants(r, body.get("roles"), after, welcome=True)
        elif kind == "service-event":
            event, data = str(body.get("event") or ""), body.get("data") if isinstance(body.get("data"), dict) else {}
            if event == "roles":
                self._on_grants(r, data.get("grants"), after, welcome=False)
            else:
                self._on_event(r, event, data, after)
        elif kind == "service-reply":
            self._on_reply(r, body, after)
        elif kind == "service-bye":
            self._set(sender, state="error", error="The service ended the connection.", grants_json="[]", connected_at=None)
            self.notifier.notify("hub", f"{r['title']} disconnected", "The service ended the connection with this instance.", "/settings/hub", "hub_service", sender)
            after.append(self._changed)

    def _verify(self, r: dict, documents: object, key: Key | None) -> tuple[list[dict], int]:
        """The role grants in `documents` that verify for us from this service, and how many didn't."""
        me = self.m.joined()
        if not isinstance(documents, list) or not me:
            return [], 0 if not documents else 1
        listed, good, bad = set(json.loads(r["roles_json"] or "[]")), {}, 0
        for doc in documents:
            try:
                if key is None:
                    raise ProtocolError("no key to check it against")
                p = P.verify_role_grant(doc, key, service=r["address"], holder=me["address"])
                if p["role"] not in listed:
                    raise ProtocolError("a role the service doesn't list")
            except (ProtocolError, ValueError, KeyError, TypeError):
                bad += 1
                continue
            old = good.get(p["role"])
            if old is None or p["iat"] > old["iat"]:
                good[p["role"]] = {"role": p["role"], "iat": p["iat"], "exp": p.get("exp"), "doc": doc}
        return list(good.values()), bad

    def _on_grants(self, r: dict, documents: object, after: list[Callable], *, welcome: bool) -> None:
        grants, bad = self._verify(r, documents, self._key(r))
        fields: dict = {"grants_json": json.dumps(grants)}
        if welcome:
            fields.update(state="connected", connected_at=r["connected_at"] if r["state"] == "connected" and r["connected_at"] else self.db.now())
        fields["error"] = f"{bad} role grant(s) from the service didn't verify and were ignored." if bad else None
        self._set(r["address"], **fields)
        after.append(self._changed)
        if bad:  # maybe the service's key rotated since we pinned it: look again, once
            after.append(lambda: self._recheck_grants(r["address"], documents))

    async def _recheck_grants(self, address: str, documents: object) -> None:
        r = self._row(address)
        if not r:
            return
        old = self._key(r)
        try:
            await self._pin(address)
        except GatewayError:
            return
        r = self._row(address)
        key = self._key(r) if r else None
        if r is None or key is None or (old is not None and key.kid == old.kid):
            return
        grants, bad = self._verify(r, documents, key)
        self._set(address, grants_json=json.dumps(grants), error=f"{bad} role grant(s) from the service didn't verify and were ignored." if bad else None)
        self._changed()

    def _on_event(self, r: dict, event: str, data: dict, after: list[Callable]) -> None:
        """Anything else a service says is an untrusted note: for the owner, and for the extensions that asked."""
        event = _clean(event, 100) or "event"
        data = self._redact(data)
        if self._event_notes.hit(r["address"]) is None:
            text = json.dumps(data, ensure_ascii=False)[:280]
            self.notifier.notify("hub", f"{r['title']}: {event}", f"From the service (untrusted): {text}", "/settings/hub", "hub_service", r["address"])
        if self.event_sink:
            service = {"address": r["address"], "kind": r["kind"], "title": r["title"]}
            after.append(lambda: self.event_sink(service, event, data))  # type: ignore[misc]

    def _on_reply(self, r: dict, body: dict, after: list[Callable]) -> None:
        call_id = str(body.get("call") or "")
        c = self.db.q1("SELECT * FROM service_calls WHERE id=? AND address=? AND state='pending'", (call_id, r["address"]))
        if not c:
            return  # unknown, answered twice, or too late
        if body.get("ok") is True:
            state, value = "ok", self._redact(body.get("result") if isinstance(body.get("result"), dict) else {})
        else:
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            state, value = "error", {"code": _clean(err.get("code"), 64) or "error", "detail": _clean(self.vault.redact(str(err.get("detail") or "")), 500)}
        self.db.ex("UPDATE service_calls SET state=?, result_json=?, done_at=? WHERE id=?", (state, json.dumps(value, ensure_ascii=False), self.db.now(), call_id))
        after.append(lambda: self._resolve(call_id, state, value))

    def _resolve(self, call_id: str, state: str, value: dict) -> None:
        fut = self._waiters.get(call_id)
        if not fut or fut.done():
            return
        try:
            on_its_loop = asyncio.get_running_loop() is fut.get_loop()
        except RuntimeError:
            on_its_loop = False
        if on_its_loop:
            fut.set_result((state, value))
        else:  # the envelope was handled on another loop (a test client's): wake the waiter safely
            fut.get_loop().call_soon_threadsafe(lambda: fut.done() or fut.set_result((state, value)))

    # ── calls ──────────────────────────────────────────────────────────────
    async def call(self, address: str, method: str, args: dict | None = None, *, by: str, role: str | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        """Call one method of a connected service and wait (up to `timeout` seconds) for its reply. -> the result (a redacted dict).
        Raises ServiceError. `by` is `agent`, `owner` or `extension:<name>`. `role` names one of the roles this instance holds for the
        service: control attaches the grant itself, and nobody who calls gets to see it."""
        if not (by in BY_RE or by.startswith("extension:")):
            raise ValueError(f"by must be agent, owner or extension:<name>, not {by!r}")
        address = self.resolve(address)
        r = self._row(address)
        if not self.m.joined() or not r or not r["enabled"] or r["state"] != "connected":
            raise ServiceError("not_connected", "That service isn't connected right now.")
        if self.paused():
            raise ServiceError("paused", "Peer activity is paused (Settings → Friends), and that includes hub services.")
        if not isinstance(method, str) or not method.strip() or len(method) > 200:
            raise ServiceError("bad_args", "A method name is 1 to 200 characters.")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ServiceError("bad_args", "The arguments are a JSON object.")
        grant = None
        if role:
            grant = next((g for g in self._usable_grants(r) if g["role"] == role), None)
            if grant is None:
                held = ", ".join(self.roles_held(r)) or "none"
                raise ServiceError("no_role", f"This instance doesn't hold the role {role!r} for {address} (held: {held}).")
        args = self._redact(args)
        method = self.vault.redact(method.strip()) or ""
        body = {"call": P.new_id(), "method": method, "args": args, "role": grant["doc"] if grant else None}
        if len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > P.MAX_SERVICE_BODY:
            raise ServiceError("too_large", "The call is over 1 MiB.")
        call_id = body["call"]
        self.db.ex("INSERT INTO service_calls(id, address, method, args_json, role, by, state, created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                   (call_id, address, method, json.dumps(args, ensure_ascii=False), role or None, by, self.db.now()))
        fut = asyncio.get_running_loop().create_future()
        self._waiters[call_id] = fut
        try:
            try:
                sent = await self._g("POST", "/v0/peer/send", {"to": address, "kind": "service-call", "body": body})
            except GatewayError as e:
                code = "not_connected" if e.error in ("not_pinned", "no_token") else "unavailable" if e.error in ("unreachable", "gateway_unreachable", "no_tunnel") else "refused"
                self._finish(call_id, "error", {"code": code, "detail": _clean(e.detail or e.error, 300)})
                raise ServiceError(code, "The call couldn't be sent.") from e
            if sent.get("state") == "failed":
                self._finish(call_id, "error", {"code": "refused", "detail": _clean(sent.get("error"), 300)})
                raise ServiceError("refused", f"The service refused the call ({sent.get('error') or 'no reason given'}).")
            try:
                state, value = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                self._finish(call_id, "timeout", None)
                raise ServiceError("timeout", f"{address} didn't answer within {timeout:g} seconds.") from None
        finally:
            self._waiters.pop(call_id, None)
        if state == "ok":
            return value
        raise ServiceError(value["code"], value.get("detail", ""), from_service=True)

    def _finish(self, call_id: str, state: str, value: dict | None) -> None:
        self.db.ex("UPDATE service_calls SET state=?, result_json=?, done_at=? WHERE id=? AND state='pending'",
                   (state, json.dumps(value) if value is not None else None, self.db.now(), call_id))

    def _fail_pending(self, why: str) -> None:
        for c in self.db.q("SELECT id FROM service_calls WHERE state='pending'"):
            self._finish(c["id"], "timeout", {"code": "timeout", "detail": why})
            fut = self._waiters.get(c["id"])
            if fut and not fut.done():
                fut.set_exception(ServiceError("timeout", why))

    def calls(self, limit: int = 50) -> list[dict]:
        """The call log, newest first (args and results as stored: redacted)."""
        return self.db.q("SELECT * FROM service_calls ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    # ── lifecycle ──────────────────────────────────────────────────────────
    def startup(self) -> None:
        """Calls that were waiting when control stopped will never be answered to anyone: they timed out."""
        for c in self.db.q("SELECT id FROM service_calls WHERE state='pending'"):
            self._finish(c["id"], "timeout", {"code": "timeout", "detail": "Control restarted while waiting."})

    async def housekeeping(self, now: datetime) -> None:
        self.db.ex("DELETE FROM service_calls WHERE created_at<?", (iso(now - KEEP_CALLS),))
        for c in self.db.q("SELECT id FROM service_calls WHERE state='pending' AND created_at<?", (iso(now - timedelta(seconds=CALL_TIMEOUT + 60)),)):
            if c["id"] not in self._waiters:
                self._finish(c["id"], "timeout", {"code": "timeout", "detail": "No answer."})
        if not self.m.joined():
            if self.db.q1("SELECT 1 FROM hub_services"):
                await self.forget_all()
            return
        if self._last_sync is None or now - self._last_sync >= RESYNC_EVERY:
            await self.sync(refresh=False)
            return
        async with self._lock:
            try:
                await self._maintain()
            except Exception:  # noqa: BLE001
                log.exception("hub services maintenance")

