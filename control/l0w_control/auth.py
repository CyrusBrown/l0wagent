"""The owner's account: password login, optional TOTP, sessions and device tokens.

- Passwords: argon2id, at least 12 characters. (The breached-password check is deferred.)
- Rate limits: too many failures from one address → 429 with Retry-After. There is no account
  lockout (a stranger could keep the owner out with it). Instead, once failures pile up
  across all addresses, an address that has never signed in successfully gets one password
  check every couple of seconds (429 otherwise).
- TOTP secrets are encrypted with the vault key; enabling 2FA issues one-time recovery codes.
- Browser sessions are cookies that expire after SESSION_DAYS without use; device tokens
  (desktop app, `l0w` CLI) last until revoked. Only hashes of either are stored.
- Device tokens have a scope (`SCOPES`): `full`, `cli` (everyday work; no account, security,
  secret, connector or settings changes) or `read` (reads only). `scope_allows` is checked
  for every owner route; browser sessions are always `full`.
- Every new session or device raises a security alert (an Inbox card) through `on_new_session`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from .api import schemas as s
from .backends.base import BadRequest, Conflict, LoginFailed, NotFound, Unauthorized
from .config import Settings
from .db import Database

MIN_PASSWORD = 12
CHALLENGE_TTL = 300
CHALLENGE_TRIES = 5
_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")
SESSION_COOKIE = "l0w_session"
SECURE_SESSION_COOKIE = "__Host-l0w_session"  # used when the cookie is Secure
SESSION_COOKIES = (SECURE_SESSION_COOKIE, SESSION_COOKIE)
SESSION_DAYS_DEFAULT = 30
SLOWDOWN_AFTER = 20  # failed logins from anywhere within the window before the global slow-down starts
SLOWDOWN_GAP = 2.0  # seconds between password checks during the slow-down
PENDING_TOTP_TTL = 600
RECOVERY_CODES = 10
_ENC = "fernet:"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


SCOPES = ("full", "cli", "read")
# What a `cli` token may not change (prefixes under /api; reads stay allowed).
CLI_DENY = ("/api/account", "/api/secrets", "/api/security-alerts", "/api/connector", "/api/connected-apps", "/api/integrations",
            "/api/settings", "/api/notifications/settings", "/api/extensions", "/api/hub", "/api/backups", "/api/membership", "/api/friends")
READ_ALLOW = ("/api/auth/logout",)  # the only write a read-only token may make


def scope_allows(scope: str, method: str, path: str) -> bool:
    """Whether a device token of this scope may make this owner-API request."""
    if scope == "full" or method in ("GET", "HEAD", "OPTIONS"):
        return True
    under = lambda prefixes: any(path == p or path.startswith(p + "/") for p in prefixes)  # noqa: E731
    if scope == "cli":
        return not under(CLI_DENY)
    return under(READ_ALLOW)


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── TOTP (RFC 6238: SHA-1, 6 digits, 30 s) ────────────────────────────────
def totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def totp_match(secret: str, code: str, at: float | None = None, window: int = 1) -> int | None:
    """Return the matching time step (for replay protection), or None."""
    code = "".join(code.split())
    now = int((at if at is not None else time.time()) // 30)
    for step in range(now - window, now + window + 1):
        if hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


BROWSERS = (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"), ("curl/", "curl"), ("python", "Python client"))
SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Windows", "Windows"), ("Linux", "Linux"))


def device_label(user_agent: str, fallback: str = "Unknown device") -> str:
    ua = user_agent or ""
    browser = next((name for key, name in BROWSERS if key in ua), None)
    system = next((name for key, name in SYSTEMS if key in ua), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or fallback


@dataclass
class _Challenge:
    want: str
    label: str | None
    expires: float
    scope: str = "full"
    tries: int = 0


class Auth:
    def __init__(self, db: Database, settings: Settings, on_new_session: Callable[[str, str, str | None], None] | None = None,
                 fernet: Fernet | None = None):
        self.db = db
        self.settings = settings
        self.on_new_session = on_new_session or (lambda *_: None)
        self.fernet = fernet  # the vault's cipher, for TOTP secrets
        self._last_check = 0.0
        self._challenges: dict[str, _Challenge] = {}

    # ── owner ──────────────────────────────────────────────────────────────
    def _user(self) -> dict | None:
        return self.db.q1("SELECT * FROM users WHERE id=1")

    def owner(self) -> s.Owner | None:
        u = self._user()
        if not u:
            return None
        passkeys = self.db.q1("SELECT COUNT(*) AS n FROM passkeys")["n"]
        return s.Owner(username=u["username"], display_name=u["display_name"], timezone=u["timezone"], totp_enabled=bool(u["totp_secret"]), passkeys=passkeys)

    def setup_owner(self, req: s.SetupOwnerRequest) -> s.Owner:
        if self._user():
            raise Conflict("already_set_up", "This instance already has an owner.")
        self._check_password(req.password)
        now = self.db.now()
        try:
            self.db.ex(
                "INSERT INTO users(id, username, display_name, password_hash, timezone, created_at, password_changed_at) VALUES(1,?,?,?,?,?,?)",
                (req.username.strip(), (req.display_name or "").strip() or None, _hasher.hash(req.password), req.timezone, now, now),
            )
        except Exception as e:  # a concurrent setup won the race
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                raise Conflict("already_set_up", "This instance already has an owner.") from e
            raise
        return self.owner()  # type: ignore[return-value]

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise BadRequest("too_short", f"Use at least {MIN_PASSWORD} characters.")

    # ── login ──────────────────────────────────────────────────────────────
    def _record(self, ip: str, username: str | None, ok: bool) -> None:
        self.db.ex("INSERT INTO login_attempts(ip, username, ok, at) VALUES(?,?,?,?)", (ip, username, int(ok), self.db.now()))

    def _throttle(self, ip: str) -> None:
        """Refuse before checking anything if this address is over its limit, or the whole
        server is under a guessing attack and this address is a stranger."""
        window = timedelta(minutes=15)
        since = iso(utcnow() - window)
        fails = self.db.q("SELECT at FROM login_attempts WHERE ip=? AND ok=0 AND at>=? ORDER BY at", (ip, since))
        if len(fails) >= self.settings.get("LOGIN_MAX_FAILURES_PER_IP"):
            # Limited for the full window after its newest failure.
            retry = int((parse(fails[-1]["at"]) + window - utcnow()).total_seconds()) + 1  # type: ignore[operator]
            raise LoginFailed(s.LoginError(error="rate_limited", retry_after_s=max(retry, 1)), 429)
        if self.db.q1("SELECT COUNT(*) AS n FROM login_attempts WHERE ok=0 AND at>=?", (since,))["n"] >= SLOWDOWN_AFTER:
            if not self.db.q1("SELECT 1 FROM login_attempts WHERE ip=? AND ok=1 LIMIT 1", (ip,)):
                now = time.monotonic()
                wait = self._last_check + SLOWDOWN_GAP - now
                if wait > 0:
                    raise LoginFailed(s.LoginError(error="rate_limited", retry_after_s=int(wait) + 1), 429)
                self._last_check = now

    def _fail(self, ip: str, username: str | None, error: str = "invalid_credentials") -> LoginFailed:
        self._record(ip, username, False)
        return LoginFailed(s.LoginError(error=error))  # type: ignore[arg-type]

    def login(self, req: s.LoginRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]:
        self._throttle(ip)
        u = self._user()
        if not u or not hmac.compare_digest(req.username.strip().lower(), u["username"].lower()):
            try:
                _hasher.verify(_DUMMY_HASH, req.password)  # same cost as a real check
            except VerificationError:
                pass
            raise self._fail(ip, req.username)
        try:
            _hasher.verify(u["password_hash"], req.password)
        except (VerificationError, InvalidHashError):
            raise self._fail(ip, u["username"]) from None
        if _hasher.check_needs_rehash(u["password_hash"]):
            self.db.ex("UPDATE users SET password_hash=? WHERE id=1", (_hasher.hash(req.password),))
        if u["totp_secret"]:
            challenge = secrets.token_urlsafe(24)
            self._challenges = {k: c for k, c in self._challenges.items() if c.expires > time.time()}
            self._challenges[challenge] = _Challenge(req.want, req.device_label, time.time() + CHALLENGE_TTL, scope=req.scope)
            return s.LoginResult(status="totp_required", challenge=challenge), None
        self._record(ip, u["username"], True)
        return self._issue(req.want, req.device_label, ip, user_agent, req.scope)

    def login_totp(self, req: s.TotpRequest, ip: str, user_agent: str) -> tuple[s.LoginResult, str | None]:
        self._throttle(ip)
        u = self._user()
        c = self._challenges.get(req.challenge)
        if not u or not u["totp_secret"] or not c or c.expires < time.time():
            self._challenges.pop(req.challenge, None)
            raise LoginFailed(s.LoginError(error="invalid_code"))
        secret = self._open_secret(u["totp_secret"])
        if not u["totp_secret"].startswith(_ENC):  # an older plaintext secret: encrypt it now
            self.db.ex("UPDATE users SET totp_secret=? WHERE id=1", (self._seal_secret(secret),))
        step = totp_match(secret, req.code)
        last = self.db.q1("SELECT value FROM kv WHERE key='totp_last_step'")
        replayed = step is not None and last is not None and step <= int(last["value"])
        if step is None or replayed:
            if not (step is None and self._burn_recovery_code(req.code)):
                c.tries += 1
                if c.tries >= CHALLENGE_TRIES:
                    self._challenges.pop(req.challenge, None)
                raise self._fail(ip, u["username"], "invalid_code")
        else:
            self._kv("totp_last_step", str(step))
        self._challenges.pop(req.challenge, None)
        self._record(ip, u["username"], True)
        return self._issue(c.want, c.label, ip, user_agent, c.scope)

    def _kv(self, key: str, value: str) -> None:
        self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                   (key, value, self.db.now()))

    def _issue(self, want: str, label: str | None, ip: str, user_agent: str, scope: str = "full") -> tuple[s.LoginResult, str | None]:
        token = secrets.token_urlsafe(32)
        sid = secrets.token_urlsafe(9)
        kind = "device" if want == "device_token" else "session"
        if kind == "session" or scope not in SCOPES:
            scope = "full"
        now = utcnow()
        expires = None if kind == "device" else iso(now + timedelta(days=self.settings.get("SESSION_DAYS")))
        name = (label or "").strip()[:80] or device_label(user_agent, "App" if kind == "device" else "Browser")
        self.db.ex(
            "INSERT INTO auth_sessions(id, kind, token_hash, label, ip, user_agent, created_at, last_seen_at, expires_at, scope) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, kind, sha256(token), name, ip, user_agent[:300], iso(now), iso(now), expires, scope),
        )
        self.on_new_session(sid, name, ip)
        if kind == "device":
            return s.LoginResult(status="ok", device_token=token), None
        return s.LoginResult(status="ok"), token

    # ── per request ────────────────────────────────────────────────────────
    def _session_row(self, cookie: str | None, bearer: str | None) -> dict | None:
        for token, kind in ((bearer, "device"), (cookie, "session")):
            if not token:
                continue
            row = self.db.q1("SELECT * FROM auth_sessions WHERE token_hash=? AND kind=? AND revoked_at IS NULL", (sha256(token), kind))
            if row and (row["expires_at"] is None or parse(row["expires_at"]) > utcnow()):  # type: ignore[operator]
                return row
        return None

    def authenticate(self, cookie: str | None, bearer: str | None) -> s.Owner:
        row = self._session_row(cookie, bearer)
        owner = self.owner()
        if not row or not owner:
            raise Unauthorized()
        now = utcnow()
        if now - parse(row["last_seen_at"]) > timedelta(minutes=5):  # type: ignore[operator]
            expires = row["expires_at"] and iso(now + timedelta(days=self.settings.get("SESSION_DAYS")))
            self.db.ex("UPDATE auth_sessions SET last_seen_at=?, expires_at=? WHERE id=?", (iso(now), expires, row["id"]))
        return owner

    def scope(self, cookie: str | None, bearer: str | None) -> str:
        """The scope of the credential that authenticated this request (call after `authenticate`)."""
        row = self._session_row(cookie, bearer)
        return (row["scope"] or "full") if row else "full"

    def create_device(self, label: str, scope: str, ip: str, user_agent: str) -> str:
        """A new device token from a signed-in session (Settings → Devices); shown once."""
        if scope not in SCOPES:
            raise BadRequest("bad_scope", f"Scope must be one of {', '.join(SCOPES)}.")
        result, _ = self._issue("device_token", label, ip, user_agent, scope)
        return result.device_token  # type: ignore[return-value]

    def current_session_id(self, cookie: str | None, bearer: str | None) -> str | None:
        row = self._session_row(cookie, bearer)
        return row["id"] if row else None

    def logout(self, cookie: str | None, bearer: str | None) -> None:
        row = self._session_row(cookie, bearer)
        if row:
            self.db.ex("UPDATE auth_sessions SET revoked_at=? WHERE id=?", (self.db.now(), row["id"]))

    def sessions(self, cookie: str | None, bearer: str | None) -> list[s.AuthSession]:
        current = self.current_session_id(cookie, bearer)
        rows = self.db.q(
            "SELECT * FROM auth_sessions WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) ORDER BY last_seen_at DESC",
            (self.db.now(),),
        )
        return [
            s.AuthSession(id=r["id"], kind=r["kind"], label=r["label"], ip=r["ip"], created_at=r["created_at"], last_seen_at=r["last_seen_at"], current=r["id"] == current,
                          scope=r["scope"] or "full")
            for r in rows
        ]

    def revoke(self, session_id: str) -> None:
        if not self.db.ex("UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (self.db.now(), session_id)):
            raise NotFound(f"session {session_id}")

    # ── account changes ────────────────────────────────────────────────────
    def _verify_password(self, password: str) -> None:
        u = self._user()
        try:
            _hasher.verify(u["password_hash"], password)  # type: ignore[index]
        except (VerificationError, InvalidHashError):
            raise BadRequest("wrong_password", "That isn't your current password.") from None

    def change_password(self, req: s.PasswordChange, cookie: str | None, bearer: str | None) -> None:
        self._verify_password(req.current_password)
        self._check_password(req.new_password)
        keep = self.current_session_id(cookie, bearer)
        now = self.db.now()
        with self.db.tx() as db:
            db.ex("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=1", (_hasher.hash(req.new_password), now))
            db.ex("UPDATE auth_sessions SET revoked_at=? WHERE revoked_at IS NULL AND id IS NOT ?", (now, keep))

    # ── TOTP secrets and recovery codes ────────────────────────────────────
    def _seal_secret(self, secret: str) -> str:
        if self.fernet is None:
            raise RuntimeError("Auth has no cipher for the TOTP secret")
        return _ENC + self.fernet.encrypt(secret.encode()).decode()

    def _open_secret(self, stored: str) -> str:
        if not stored.startswith(_ENC):
            return stored  # written before secrets were encrypted
        try:
            return self.fernet.decrypt(stored[len(_ENC):].encode()).decode()  # type: ignore[union-attr]
        except (InvalidToken, AttributeError) as e:
            raise RuntimeError("The TOTP secret can't be decrypted (was the secret key changed?). Reset 2FA with ./l0w.") from e

    @staticmethod
    def _recovery_hash(code: str) -> str:
        return sha256("recovery:" + "".join(ch for ch in code.lower() if ch.isalnum()))

    def _burn_recovery_code(self, code: str) -> bool:
        if not any(ch.isalpha() for ch in code):
            return False  # a digit-only code is a TOTP attempt
        return bool(self.db.ex("UPDATE recovery_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL", (self.db.now(), self._recovery_hash(code))))

    def _new_recovery_codes(self) -> list[str]:
        codes = []
        for _ in range(RECOVERY_CODES):
            raw = base64.b32encode(secrets.token_bytes(7)).decode().rstrip("=").lower()[:10]
            codes.append(f"{raw[:5]}-{raw[5:]}")
        with self.db.tx() as db:
            db.ex("DELETE FROM recovery_codes")
            for c in codes:
                db.ex("INSERT INTO recovery_codes(code_hash) VALUES(?)", (self._recovery_hash(c),))
        return codes

    def totp_setup(self) -> s.TotpSetup:
        import segno

        u = self._user()
        secret = totp_secret()
        self._kv("totp_pending", f"{int(time.time()) + PENDING_TOTP_TTL}|{self._seal_secret(secret)}")
        label = quote(f"l0wagent:{u['username']}")  # type: ignore[index]
        uri = f"otpauth://totp/{label}?secret={secret}&issuer=l0wagent"
        buf = io.BytesIO()
        # The web app shows this as an <img>, which only renders an SVG that names its namespace
        # (svg_inline() leaves it out).
        segno.make(uri, error="m").save(buf, kind="svg", xmldecl=False, svgns=True, nl=False, scale=4, dark="#1f1e1d", light="#ffffff")
        svg = buf.getvalue().decode()
        return s.TotpSetup(secret=secret, otpauth_uri=uri, qr_svg=svg)

    def totp_enable(self, req: s.TotpEnable) -> s.TotpEnabled:
        """Verifies against the secret control issued in `totp_setup`, never one the client sends."""
        row = self.db.q1("SELECT value FROM kv WHERE key='totp_pending'")
        expires, _, sealed = (row["value"] if row else "").partition("|")
        if not sealed or int(expires) < time.time():
            raise BadRequest("invalid_code", "That setup expired. Start again to get a new QR code.")
        secret = self._open_secret(sealed)
        step = totp_match(secret, req.code)
        if step is None:
            raise BadRequest("invalid_code", "That code didn't match. Check your device's clock and try the next code.")
        self.db.ex("UPDATE users SET totp_secret=? WHERE id=1", (self._seal_secret(secret),))
        self.db.ex("DELETE FROM kv WHERE key='totp_pending'")
        self._kv("totp_last_step", str(step))
        return s.TotpEnabled(**self.owner().model_dump(), recovery_codes=self._new_recovery_codes())  # type: ignore[union-attr]

    def totp_disable(self, req: s.TotpDisable) -> s.Owner:
        self._verify_password(req.password)
        self.db.ex("UPDATE users SET totp_secret=NULL WHERE id=1")
        self.db.ex("DELETE FROM recovery_codes")
        self.db.ex("DELETE FROM kv WHERE key='totp_pending'")
        return self.owner()  # type: ignore[return-value]
