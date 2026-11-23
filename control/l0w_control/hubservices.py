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
from urllib.parse import urlsplit

import httpx
from l0wproto import b64 as B64
from l0wproto import payloads as P
from l0wproto import spaces as SP
from l0wproto.errors import ProtocolError
from l0wproto.keys import Key
from l0wproto.times import parse_time

from . import __version__
from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import Conflict, NotFound
from .friends import Window
from .membership import GatewayError
from .safefetch import PinnedAsyncTransport

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
ERROR_DATA_MAX = 64 * 1024  # bytes of a service's error `data` we keep
TRANSFER_PUT_MAX = 128 * 1024 * 1024  # the most a transfer_put sends (spaces.md: max_file is 95 MiB by default)
TRANSFER_TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)
HOSTNAMES_TTL = 300.0  # seconds a service's directory hostnames are remembered for transfers


class ServiceError(Exception):
    """A call that didn't give a result. `code` is the service's own error code (`not_allowed`, `unknown_method`, ...) when it
    answered, else one of control's: `unknown_service`, `not_connected`, `no_role`, `bad_args`, `too_large`, `paused`,
    `unavailable`, `refused`, `timeout`; and for the spaces helpers `bad_signature`, `not_for_us`, `key_changed`, `unknown_member`,
    `bad_url`, `transfer_failed`. `from_service` says which. `data` is the rest of the service's error object (untrusted, redacted)."""

    def __init__(self, code: str, detail: str = "", *, from_service: bool = False, data: dict | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.from_service = code, detail, from_service
        self.data: dict = data if isinstance(data, dict) else {}


def _clean(text: object, limit: int = TEXT_MAX) -> str:
    """Text from the hub or a service, for storage: printable, one length."""
    return "".join(c for c in str(text or "") if c.isprintable() or c in "\n\t")[:limit]


class Hubservices:
    def __init__(self, db, live, vault, settings, membership, notifier, now: Callable[[], datetime] = utcnow):
        self.db, self.live, self.vault, self.settings, self.m, self.notifier, self.now = db, live, vault, settings, membership, notifier, now
        self.event_sink: Callable[[dict, str, dict], int] | None = None  # set by the backend: hands an event to the extensions
        self._lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future] = {}
        self.ext_changed: Callable[[], None] | None = None  # set by ops.py: the rows changed, so installed extensions may need to follow
        self._pushed: list[str] | None = None  # the service addresses last given to the gateway
        self._last_sync: datetime | None = None
        self._hostnames: dict[str, tuple[float, str]] = {}  # service address -> (when, its instance hostname), for transfers
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
            extension_name=r["address"].split("@", 1)[0] if ext else None, extension_enabled=bool(r["extension_enabled"]),
            extension_state=r["ext_state"] if ext else None, extension_commit=r["ext_commit"] if ext else None, extension_error=r["ext_error"] if ext else None,
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
        self._ext_changed()

    def _ext_changed(self) -> None:
        if self.ext_changed:
            self.ext_changed()

    async def _sync(self, refresh: bool) -> None:
        r = self.m.joined()
        if not r or not self.m.gw:
            return
        try:
            hub_key = Key.from_dict(r["hub_key"])
        except (ValueError, KeyError, TypeError, ProtocolError):
            return
        listed, skipped = P.services_in(r["capabilities"] or {}, hub_key)
        docs = self._raw_docs(r["capabilities"] or {}, hub_key)
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
                off = addr in self.m.join_services_off  # the owner switched it off in the join form
                on = 0 if off else fields["default_on"]
                self.db.ex("INSERT INTO hub_services(address, kind, title, description, docs, roles_json, extension_json, default_on, enabled, extension_enabled, doc_json,"
                           " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (addr, *fields.values(), on, on, docs.get(addr), now, now))
                changed = True
                if late:
                    state = " and it's on." if on else "; it's off until you turn it on."
                    self.notifier.notify("hub", f"New hub service: {fields['title']}", "Your hub added it" + state, "/settings/hub", "hub_service", addr)
            else:
                if docs.get(addr) != cur["doc_json"]:  # the signed document as received: kept for ops, which checks it again
                    self.db.ex("UPDATE hub_services SET doc_json=? WHERE address=?", (docs.get(addr), addr))
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

    @staticmethod
    def _raw_docs(caps: dict, hub_key: Key) -> dict[str, str]:
        """The signed service documents in the capabilities, as received (JSON text), by lowercase address."""
        out: dict[str, str] = {}
        for e in caps.get("extras") or []:
            if not isinstance(e, dict) or e.get("kind") != "service" or not isinstance(e.get("doc"), dict):
                continue
            try:
                p = P.verify_service(e["doc"], hub_key, hub_domain=caps.get("domain") if isinstance(caps.get("domain"), str) else None)
            except (ProtocolError, ValueError, KeyError, TypeError):
                continue
            out.setdefault(p["address"].lower(), json.dumps(e["doc"], separators=(",", ":"), sort_keys=True))
        return out

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
        """The owner's switch for the service."""
        return await self.patch(address, enabled=enabled)

    async def patch(self, address: str, *, enabled: bool | None = None, extension_enabled: bool | None = None) -> s.HubService:
        """The owner's switches: the service's (connect or say goodbye), and its extension's (installed in the background by ops.py)."""
        async with self._lock:
            if not self.m.joined():
                raise Conflict("not_joined", "This instance isn't a member of a hub.")
            r = self._row(address)
            if not r:
                raise NotFound(f"hub service {address}")
            if extension_enabled is not None:
                if not r["extension_json"]:
                    raise Conflict("no_extension", "This service has no extension.")
                if extension_enabled and not r["extension_enabled"]:
                    self._set(r["address"], extension_enabled=1, ext_state="installing", ext_error=None)
                    self._ext_changed()
                elif extension_enabled and r["ext_state"] == "waiting":  # "Update now": the owner agrees to the newest commit
                    self._ext_approve(r)
                elif not extension_enabled and r["extension_enabled"]:
                    self._set(r["address"], extension_enabled=0)
                    self._ext_changed()
            if enabled is not None:
                self._set(r["address"], enabled=1 if enabled else 0)
                if enabled:
                    if r["state"] in ("off", "error"):
                        await self._connect(r["address"])
                else:
                    await self._teardown(r["address"])
            self._changed()
            return self.model(self._row(address))  # type: ignore[arg-type]

    def _ext_approve(self, r: dict) -> None:
        ext = json.loads(r["extension_json"])
        self.db.ex("UPDATE hub_services SET ext_approved=? WHERE address=?", (ext["commit"], r["address"]))
        self._ext_changed()

    async def forget_all(self) -> None:
        """This instance left its hub: nothing of its services stays."""
        async with self._lock:
            self.db.ex("DELETE FROM hub_services")
            self._pushed = None
            self._fail_pending("This instance left its hub.")
            self._changed()
        self._ext_changed()  # what the hub's services installed goes with them

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
            extra = {k: v for k, v in err.items() if k not in ("code", "detail")}  # `head` of a conflict, `lock` of a locked...: untrusted data
            if extra:
                extra = self._redact(extra)
                if len(json.dumps(extra, ensure_ascii=False).encode("utf-8")) <= ERROR_DATA_MAX:
                    value["data"] = extra
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
        raise ServiceError(value["code"], value.get("detail", ""), from_service=True, data=value.get("data"))

    # ── shared spaces: what an extension needs from the instance (protocol/spaces.md) ────────────────────
    def _need_hub(self) -> dict:
        r = self.m.joined()
        if not r or self.m.gw is None:
            raise ServiceError("not_joined", "This instance hasn't joined a hub.")
        return r

    @staticmethod
    def _gw_service_error(e: GatewayError) -> ServiceError:
        code = {"bad_request": "bad_args", "no_keys": "not_joined", "no_tunnel": "not_joined", "no_such_member": "unknown_member", "key_changed": "key_changed",
                "not_for_us": "not_for_us", "not_the_author": "refused", "not_the_granter": "refused", "cannot_open": "bad_key", "too_large": "too_large",
                "unreachable": "unavailable", "gateway_unreachable": "unavailable", "bad_directory": "unavailable", "hub_capabilities": "unavailable",
                "hub_key_changed": "key_changed"}.get(e.error, "refused")
        return ServiceError(code, _clean(e.detail or e.error, 300))

    async def sign_space_version(self, payload: dict) -> str:
        """Have the gateway sign a `space-version` payload as this instance. -> the signed document as compact JSON text (the bytes a version's id hashes)."""
        self._need_hub()
        if not isinstance(payload, dict):
            raise ServiceError("bad_args", "The payload is an object.")
        try:
            out = await self._g("POST", "/v0/sign/space-version", {"payload": payload})
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        doc = out.get("doc")
        if not isinstance(doc, dict):
            raise ServiceError("unavailable", "The gateway gave no document.")
        return SP.version_doc_bytes(doc).decode("utf-8")

    async def sign_space_key(self, payload: dict) -> str:
        """Have the gateway sign a `space-key` payload this instance grants (granter = us, granter_kid = our current signing key).
        -> the signed document as compact JSON text. The payload carries the key already sealed to the member; nothing secret passes here."""
        self._need_hub()
        if not isinstance(payload, dict):
            raise ServiceError("bad_args", "The payload is an object.")
        try:
            out = await self._g("POST", "/v0/sign/space-key", {"payload": payload})
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        doc = out.get("doc")
        if not isinstance(doc, dict):
            raise ServiceError("unavailable", "The gateway gave no document.")
        return json.dumps(doc, separators=(",", ":"))

    async def member_keys(self, address: str) -> dict:
        """A hub member's public keys, pinned at the gateway (the first time; later only through signed rotations):
        {address, sign, seal} as key dicts. For sealing to a member and verifying what it signed (spaces.md)."""
        self._need_hub()
        if not isinstance(address, str) or "@" not in address:
            raise ServiceError("bad_args", "Name a member by address.")
        try:
            pin = await self._g("POST", "/v0/peer/pin", {"address": address.strip().lower()})
            out = {"address": str(pin["address"]).lower(), "sign": Key.from_dict(pin["sign_key"]).to_dict(), "seal": Key.from_dict(pin["seal_key"]).to_dict()}
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        except (KeyError, TypeError, ValueError, ProtocolError):
            raise ServiceError("unavailable", "The directory gave no usable keys for that member.") from None
        return out

    def my_keys(self) -> dict:
        """This instance's address and current public keys {address, sign, seal}, from its own self-description."""
        r = self._need_hub()
        doc = self.m.self_doc()
        try:
            p = json.loads(B64.decode(doc["payload"])) if doc else None
            ks = {k["use"]: Key.from_dict(k) for k in p["keys"]} if p else {}
            return {"address": r["address"], "sign": ks["sign"].to_dict(), "seal": ks["seal"].to_dict()}
        except (KeyError, TypeError, ValueError, ProtocolError):
            raise ServiceError("not_joined", "This instance's self-description isn't published yet.") from None

    async def open_space_key(self, doc: str | dict, creator: str) -> bytes:
        """Open a `space-key` document addressed to this instance -> the space key. The document's signature is checked here against the
        granter's directory entry (pinned at the gateway, which also follows its rotations); the gateway then opens it with the seal key."""
        me = self._need_hub()
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                raise ServiceError("bad_args", "The document isn't JSON.") from None
        if not isinstance(creator, str) or not creator.strip():
            raise ServiceError("bad_args", "Name the space's creator (an address).")
        creator = creator.strip().lower()
        try:
            p = SP.space_key_payload(doc)
            if p["granter"].lower() != creator:
                raise ServiceError("bad_signature", "The key wasn't granted by the space's creator.")
            if p["member"].lower() != me["address"].lower():
                raise ServiceError("not_for_us", "The key is for another member.")
        except ProtocolError as e:
            raise ServiceError("bad_args", _clean(f"Not a space-key document: {e.detail}", 300)) from None
        try:
            pin = await self._g("POST", "/v0/peer/pin", {"address": creator})
            key = Key.from_dict(pin["sign_key"])
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        except (KeyError, TypeError, ValueError, ProtocolError):
            raise ServiceError("unavailable", "The directory gave no usable signing key for the creator.") from None
        try:
            SP.verify_space_key(doc, key, member=me["address"], granter=creator)
        except ProtocolError as e:
            raise ServiceError("bad_signature", _clean(f"The key document doesn't verify: {e.detail}", 300)) from None
        try:
            out = await self._g("POST", "/v0/space/open-key", {"doc": doc})
            k = B64.decode(out.get("key"))
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        except ProtocolError:
            raise ServiceError("unavailable", "The gateway gave no key.") from None
        if len(k) != 32:
            raise ServiceError("unavailable", "The gateway gave a key of the wrong size.")
        return k

    async def _instance_host(self, address: str) -> str:
        """The service's own instance hostname, from its directory entry (remembered for a few minutes)."""
        now = self.now().timestamp()
        got = self._hostnames.get(address)
        if got and now - got[0] < HOSTNAMES_TTL:
            return got[1]
        try:
            pin = await self._pin(address)
        except GatewayError as e:
            raise self._gw_service_error(e) from e
        host = str((pin.get("hostnames") or {}).get("instance") or "").strip().lower()
        if not host:
            raise ServiceError("unavailable", "The directory gave no hostname for the service.")
        self._hostnames[address] = (now, host)
        return host

    @staticmethod
    def _bare(netloc: str, scheme: str) -> str:
        return netloc.lower().removesuffix(":443" if scheme == "https" else ":80")

    async def _transfer_url(self, service: str, url: str) -> tuple[str, dict]:
        """-> (the URL to request, extra headers), refusing anything but a URL on the connected service's own instance hostname."""
        address = self.resolve(service)
        r = self._row(address)
        if not self.m.joined() or not r or not r["enabled"] or r["state"] != "connected":
            raise ServiceError("not_connected", "That service isn't connected right now.")
        if self.paused():
            raise ServiceError("paused", "Peer activity is paused (Settings → Friends), and that includes hub services.")
        try:
            u = urlsplit(str(url))
            _ = u.port
        except ValueError:
            raise ServiceError("bad_url", "That isn't a URL.") from None
        if u.scheme != "https" and not (self.m.allow_http and u.scheme == "http"):
            raise ServiceError("bad_url", "A transfer URL is https.")
        if not u.netloc or "@" in u.netloc:
            raise ServiceError("bad_url", "A transfer URL has no user name or password.")
        host = await self._instance_host(address)
        if self._bare(u.netloc, u.scheme) != self._bare(host, u.scheme):
            raise ServiceError("bad_url", "That URL isn't on the service's own hostname.")
        if self.m.connect_to:  # development: the fake hub answers on one address (membership does the same), with the real Host header
            return u._replace(netloc=self.m.connect_to).geturl(), {"Host": u.netloc}
        return str(url), {}

    def _transfer_client(self) -> httpx.AsyncClient:
        transport = self.m.transport
        if transport is None and not (self.m.allow_http or self.m.connect_to):
            transport = PinnedAsyncTransport()  # vetted like control's other outgoing calls: public addresses only
        return httpx.AsyncClient(timeout=TRANSFER_TIMEOUT, transport=transport, follow_redirects=False, trust_env=False)

    async def transfer_put(self, service: str, url: str, data: bytes) -> None:
        """PUT `data` to a transfer URL on the service's own hostname. No redirects, no credentials."""
        if not isinstance(data, (bytes, bytearray)):
            raise ServiceError("bad_args", "The data is bytes.")
        if len(data) > TRANSFER_PUT_MAX:
            raise ServiceError("too_large", "That is over 128 MiB.")
        target, extra = await self._transfer_url(service, url)
        try:
            async with self._transfer_client() as c:
                r = await c.put(target, content=bytes(data), headers={"Content-Type": "application/octet-stream", **extra})
        except httpx.HTTPError as e:
            raise ServiceError("transfer_failed", f"Couldn't reach the service ({e.__class__.__name__}).") from e
        if not 200 <= r.status_code < 300:
            raise ServiceError("transfer_failed", f"The service answered {r.status_code}.")

    async def transfer_get(self, service: str, url: str, max_bytes: int) -> bytes:
        """GET a transfer URL on the service's own hostname, at most `max_bytes`. No redirects, no credentials."""
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ServiceError("bad_args", "max_bytes is a whole number of bytes.")
        target, extra = await self._transfer_url(service, url)
        buf = bytearray()
        try:
            async with self._transfer_client() as c, c.stream("GET", target, headers=extra) as r:
                if not 200 <= r.status_code < 300:
                    raise ServiceError("transfer_failed", f"The service answered {r.status_code}.")
                if int(r.headers.get("content-length") or 0) > max_bytes:
                    raise ServiceError("too_large", f"The download is over {max_bytes} bytes.")
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise ServiceError("too_large", f"The download is over {max_bytes} bytes.")
        except httpx.HTTPError as e:
            raise ServiceError("transfer_failed", f"Couldn't reach the service ({e.__class__.__name__}).") from e
        except ValueError:
            raise ServiceError("transfer_failed", "The service sent a bad Content-Length.") from None
        return bytes(buf)

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

