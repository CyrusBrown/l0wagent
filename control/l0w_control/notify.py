"""Notifications: web push to the owner's browsers and installed apps, and ntfy as a fallback.

- **Web push** needs the app on HTTPS with a valid certificate (a hub, or Tailscale HTTPS);
  browsers refuse it on plain HTTP. Control signs pushes with its own VAPID key (made on
  first use, kept in the data dir) and sends them straight to the browser's push service.
- **ntfy** (optional) posts to a topic URL the owner chose, for plain-HTTP installs. By default it
  carries only the title (a public topic is readable by anyone); message previews are opt-in. An
  optional access token (a vault secret, never returned) goes out as a bearer token. The URL is
  vetted like any outbound URL (safefetch): LAN servers are fine, this install's own services aren't.
- **What notifies** is per event type (Settings → Notifications). On by default: anything
  waiting for your approval (permissions, secure input, promote requests) and new-device
  sign-ins. Opt-in: questions, tasks, assistant messages, failed runs.
- **Quiet hours** hold back everything except approvals and new devices.

Every notification is also stored (`notifications`), so a missed push isn't lost information:
the Inbox already shows what's waiting. Bodies never include secret values (they're redacted
before they get here) or message text beyond a short preview.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .api import schemas as s
from .backends.base import BadRequest
from .db import Database
from .safefetch import PinnedAsyncTransport, vet_url

log = logging.getLogger("l0w.notify")
EVENTS = {
    "approval": ("Approvals", "Permissions, secure input and promote requests waiting for you", True),
    "new_device": ("New sign-ins", "A new device or browser signed in to your account", True),
    "question": ("Questions", "The assistant asked you something", False),
    "task": ("Tasks", "Something only you can do", False),
    "assistant_message": ("Messages", "The assistant wrote in a thread", False),
    "run_failed": ("Failed runs", "A run ended with an error", False),
    "disk_low": ("Low disk space", "The server's free disk space dropped below your warning level", True),
}
ASKS = ("approval", "question", "task")  # what an agent can raise; capped per hour so a loop can't flood the owner
MAX_ASK_NOTIFICATIONS_PER_HOUR = 10
NTFY_TOKEN = "NTFY_TOKEN"
ALWAYS = ("approval", "new_device")  # not held back by quiet hours


class Notifier:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.key_path = data_dir / "vapid_private.pem"
        self.loop: asyncio.AbstractEventLoop | None = None
        self._vapid = None
        self.ntfy_transport: httpx.AsyncBaseTransport | None = None  # tests
        self.vault = None  # set by the backend: holds the ntfy token

    # ── keys and settings ──────────────────────────────────────────────────
    def vapid(self):
        if self._vapid is None:
            from py_vapid import Vapid01

            if self.key_path.exists():
                self._vapid = Vapid01.from_file(str(self.key_path))
            else:
                v = Vapid01()
                v.generate_keys()
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                v.save_key(str(self.key_path))
                self.key_path.chmod(0o600)
                self._vapid = v
        return self._vapid

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self.vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def settings(self) -> s.NotificationSettings:
        row = self.db.q1("SELECT value FROM kv WHERE key='notification_settings'")
        saved = json.loads(row["value"]) if row else {}
        events = {k: saved.get("events", {}).get(k, default) for k, (_, _, default) in EVENTS.items()}
        return s.NotificationSettings(events=events, ntfy_url=saved.get("ntfy_url"), ntfy_previews=bool(saved.get("ntfy_previews")),
                                      ntfy_token_saved=bool(self.vault and self.vault.exists(NTFY_TOKEN)), quiet_start=saved.get("quiet_start"), quiet_end=saved.get("quiet_end"),
                                      event_labels={k: f"{label}: {desc}" for k, (label, desc, _) in EVENTS.items()},
                                      subscriptions=[s.PushSubscriptionInfo(id=r["id"], label=r["label"], created_at=r["created_at"], last_ok_at=r["last_ok_at"], failures=r["failures"])  # type: ignore[arg-type]
                                                     for r in self.db.q("SELECT * FROM push_subscriptions ORDER BY id")])

    def save_settings(self, req: s.NotificationSettings) -> s.NotificationSettings:
        url = (req.ntfy_url or "").strip() or None
        if url:
            vet_url(url, allow_private=True)  # raises BadRequest for this install's own services, loopback, plain http on the internet
        if req.ntfy_token is not None:
            if self.vault is None:
                raise BadRequest("no_vault", "Can't store a token here.")
            if req.ntfy_token.strip():
                self.vault.set(NTFY_TOKEN, req.ntfy_token.strip(), "ntfy access token", "integrations")
            elif self.vault.exists(NTFY_TOKEN):
                self.vault.delete(NTFY_TOKEN)
        data = {"events": {k: bool(v) for k, v in req.events.items() if k in EVENTS}, "ntfy_url": url, "ntfy_previews": bool(req.ntfy_previews),
                "quiet_start": req.quiet_start or None, "quiet_end": req.quiet_end or None}
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('notification_settings', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (json.dumps(data), self.db.now()))
        return self.settings()

    def subscribe(self, req: s.PushSubscribe) -> None:
        self.db.ex("INSERT INTO push_subscriptions(endpoint, p256dh, auth, label, created_at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, label=excluded.label, failures=0",
                   (req.endpoint, req.keys.p256dh, req.keys.auth, req.label[:80], self.db.now()))

    def unsubscribe(self, sub_id: int | None = None, endpoint: str | None = None) -> None:
        self.db.ex("DELETE FROM push_subscriptions WHERE id=? OR endpoint=?", (sub_id, endpoint))

    def _quiet(self, tz: str) -> bool:
        st = self.settings()
        if not st.quiet_start or not st.quiet_end:
            return False
        now = datetime.now(ZoneInfo(tz)).strftime("%H:%M")
        a, b = st.quiet_start, st.quiet_end
        return (a <= now < b) if a < b else (now >= a or now < b)

    # ── sending ────────────────────────────────────────────────────────────
    def notify(self, kind: str, title: str, body: str = "", url: str = "/", ref_kind: str | None = None, ref_id: str | None = None) -> None:
        """Record a notification and send it if the owner wants this kind. Safe from any thread."""
        nid = self.db.ex("INSERT INTO notifications(kind, ref_kind, ref_id, title, body, url, created_at) VALUES(?,?,?,?,?,?,?)",
                         (kind, ref_kind, ref_id, title[:200], body[:300], url, self.db.now()))
        st = self.settings()
        if not st.events.get(kind):
            return
        if kind in ASKS:
            since = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            marks = ",".join("?" * len(ASKS))
            n = self.db.q1(f"SELECT COUNT(*) AS n FROM notifications WHERE kind IN ({marks}) AND created_at>=?", (*ASKS, since))["n"]
            if n > MAX_ASK_NOTIFICATIONS_PER_HOUR + 1:
                return  # already folded into the one below
            if n == MAX_ASK_NOTIFICATIONS_PER_HOUR + 1:
                kind, title, body, url, ref_kind, ref_id = "approval", "More waiting for you", "More asks are waiting; the rest are in your inbox.", "/inbox", "folded", "asks"
        tz = (self.db.q1("SELECT timezone FROM users WHERE id=1") or {}).get("timezone") or "UTC"
        if kind not in ALWAYS and self._quiet(tz):
            return
        payload = {"title": title[:200], "body": body[:300], "url": url, "tag": f"{ref_kind or kind}-{ref_id or nid}"}
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send(nid, payload, st.ntfy_url, st.ntfy_previews), self.loop)

    async def _send(self, nid: int, payload: dict, ntfy_url: str | None, previews: bool = False) -> int:
        sent = 0
        subs = self.db.q("SELECT * FROM push_subscriptions")
        for sub in subs:
            ok = await asyncio.to_thread(self._push, sub, payload)
            sent += int(ok)
        if ntfy_url:
            try:
                headers = {"Title": payload["title"], "Click": payload["url"]}
                token = self.vault.value_for_control(NTFY_TOKEN) if self.vault and self.vault.exists(NTFY_TOKEN) else None
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                content = (payload["body"] if previews else "") or payload["title"]
                async with httpx.AsyncClient(timeout=10, transport=self.ntfy_transport or PinnedAsyncTransport(allow_private=True), follow_redirects=False) as c:
                    await c.post(ntfy_url, content=content.encode(), headers=headers)
                sent += 1
            except httpx.HTTPError as e:
                log.warning("ntfy failed: %s", e)
        if sent:
            self.db.ex("UPDATE notifications SET sent_at=? WHERE id=?", (self.db.now(), nid))
        return sent

    def _push(self, sub: dict, payload: dict) -> bool:
        from pywebpush import WebPushException, webpush

        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}, data=json.dumps(payload),
                    vapid_private_key=self.vapid(), vapid_claims={"sub": "mailto:owner@l0wagent.invalid"}, ttl=24 * 3600, timeout=10)
        except WebPushException as e:
            gone = e.response is not None and e.response.status_code in (404, 410)
            if gone:
                self.unsubscribe(sub_id=sub["id"])
            else:
                self.db.ex("UPDATE push_subscriptions SET failures=failures+1 WHERE id=?", (sub["id"],))
            log.warning("push failed (%s): %s", "expired" if gone else "error", e)
            return False
        except Exception as e:  # noqa: BLE001
            self.db.ex("UPDATE push_subscriptions SET failures=failures+1 WHERE id=?", (sub["id"],))
            log.warning("push failed: %s", e)
            return False
        self.db.ex("UPDATE push_subscriptions SET last_ok_at=?, failures=0 WHERE id=?", (self.db.now(), sub["id"]))
        return True

    async def test(self) -> int:
        nid = self.db.ex("INSERT INTO notifications(kind, title, body, url, created_at) VALUES('test', ?, ?, '/settings/notifications', ?)",
                         ("Test notification", "Notifications from l0wagent reach this device.", self.db.now()))
        return await self._send(nid, {"title": "Test notification", "body": "Notifications from l0wagent reach this device.", "url": "/settings/notifications",
                                      "tag": "test"}, self.settings().ntfy_url, self.settings().ntfy_previews)
