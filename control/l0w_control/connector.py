"""The MCP connector: outside chat services (Claude Desktop, claude.ai, ChatGPT, opencode, Cursor)
act as the 1:1 chat through /mcp, with the chat's tools (tools.py) limited to the granted scopes.

OAuth 2.1, as the MCP spec asks:
- Discovery: /.well-known/oauth-protected-resource (RFC 9728) and
  /.well-known/oauth-authorization-server (RFC 8414). /mcp answers 401 with a pointer to them.
- Clients register themselves (/oauth/register, RFC 7591): public clients, PKCE S256 required,
  redirect URIs over https or to the loopback address only.
- /oauth/authorize checks the client and redirect URI and sends the browser to the web app's
  consent screen (/connect). The owner, logged in, approves there (POST /api/connector/consent,
  cookie + X-L0W-Client); control redirects back with a one-time code (10 minutes).
- /oauth/token swaps the code (with the PKCE verifier) for an access token (1 hour) and a
  refresh token (30 days, rotated on use). Tokens are random and stored only as SHA-256 hashes.
- Revoking a connected app (owner API) revokes its grant and every token at once.

Each grant has its own chat ("<client> (connector)"): every tool call is logged there and every
card the outside model proposes appears there for the owner to act on. Secrets, approvals and
settings are never available through the connector (tools.py only proposes them as cards).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from . import __version__
from . import tools as T
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, NotFound, Unauthorized
from .db import Database

SCOPES = {"read": "Read your projects, threads, runs, files and the assistant's plan",
          "write": "Create threads and projects and post as you (marked “via connector”), and propose cards you approve in l0wagent",
          "wake": "Wake the assistant with Reply now (spends the reply budget)"}
SCOPE_TOOLS = {"read": {"read"}, "write": {"write", "card"}, "wake": {"wake"}}
CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
MAX_CLIENTS = 100
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")
INSTRUCTIONS = """\
This is the owner's l0wagent: a long-running personal assistant on their own server. You act as their 1:1 chat
with it. Read before you answer (status, projects, threads, the plan); use `help` for how l0wagent works.
Hand work that needs the assistant's computer, time or memory to it with propose_handoff, or create a thread.
Cite threads by the links the tools return. You can't change the harness: secrets, settings, budgets, network
rules and extra wakeups are only proposed as cards, which the owner approves in l0wagent."""


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def check_redirect(uri: str) -> bool:
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if u.fragment or not u.netloc:
        return False
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)


def _scopes(text: str | None) -> list[str]:
    asked = [x for x in (text or "").split() if x]
    if not asked:
        return ["read"]
    bad = [x for x in asked if x not in SCOPES]
    if bad:
        raise OAuthError("invalid_scope", f"Unknown scope: {' '.join(bad)}. Scopes: {' '.join(SCOPES)}.")
    return [x for x in SCOPES if x in asked]


class Connector:
    def __init__(self, db: Database, be: Any):
        self.db, self.be = db, be

    # ── registration and consent ───────────────────────────────────────────
    def register(self, body: dict) -> dict:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10 or not all(isinstance(u, str) and check_redirect(u) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris: 1–10 URIs over https, or http to localhost/127.0.0.1.")
        if body.get("token_endpoint_auth_method", "none") != "none":
            raise OAuthError("invalid_client_metadata", "Only public clients (token_endpoint_auth_method none, with PKCE) are supported.")
        if (self.db.q1("SELECT COUNT(*) AS n FROM oauth_clients") or {}).get("n", 0) >= MAX_CLIENTS:
            raise OAuthError("invalid_client_metadata", "Too many registered clients; revoke some in Tools & Secrets → Connected apps.")
        name = " ".join(str(body.get("client_name") or "An MCP client").split())[:60]
        cid = "l0w-" + secrets.token_urlsafe(18)
        now = self.db.now()
        self.db.ex("INSERT INTO oauth_clients(client_id, name, redirect_uris, created_at) VALUES(?,?,?,?)", (cid, name, json.dumps(uris), now))
        return {"client_id": cid, "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "client_id_issued_at": int(utcnow().timestamp())}

    def _client(self, client_id: str | None) -> dict:
        row = self.db.q1("SELECT * FROM oauth_clients WHERE client_id=?", (client_id or "",))
        if not row:
            raise OAuthError("invalid_client", "Unknown client_id: register first.", 401)
        return row

    def check_request(self, q: dict) -> dict:
        """Validate an authorization request (both before the consent screen and on consent)."""
        client = self._client(q.get("client_id"))
        redirect = q.get("redirect_uri") or ""
        if redirect not in json.loads(client["redirect_uris"]):
            raise OAuthError("invalid_request", "redirect_uri isn't one the client registered.")
        if q.get("response_type", "code") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code.")
        challenge = q.get("code_challenge") or ""
        if q.get("code_challenge_method") != "S256" or not re.match(r"^[A-Za-z0-9_-]{43,128}$", challenge):
            raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        return {"client_id": client["client_id"], "client_name": client["name"], "redirect_uri": redirect, "code_challenge": challenge,
                "scopes": _scopes(q.get("scope")), "state": q.get("state"), "resource": q.get("resource")}

    def describe(self, q: dict) -> dict:
        """For the consent screen."""
        r = self.check_request(q)
        return {"client_name": r["client_name"], "redirect_host": urlparse(r["redirect_uri"]).netloc,
                "scopes": [{"scope": x, "description": SCOPES[x]} for x in r["scopes"]]}

    def consent(self, q: dict, approve: bool, scopes: list[str] | None = None) -> str:
        """The owner's answer. Returns the URL to send the browser to."""
        r = self.check_request(q)
        params: dict[str, str] = {}
        if r["state"]:
            params["state"] = r["state"]
        if not approve:
            params["error"] = "access_denied"
            return r["redirect_uri"] + ("&" if "?" in r["redirect_uri"] else "?") + urlencode(params)
        granted = [x for x in r["scopes"] if scopes is None or x in scopes] or ["read"]
        now = utcnow()
        chat_id = self._chat(r["client_name"])
        with self.db.tx() as db:
            gid = db.ex("INSERT INTO oauth_grants(client_id, scopes, chat_id, created_at) VALUES(?,?,?,?)",
                        (r["client_id"], " ".join(granted), chat_id, iso(now)))
            code = secrets.token_urlsafe(32)
            db.ex("INSERT INTO oauth_codes(code_hash, grant_id, redirect_uri, challenge, resource, expires_at) VALUES(?,?,?,?,?,?)",
                  (_hash(code), gid, r["redirect_uri"], r["code_challenge"], r["resource"], iso(now + CODE_TTL)))
        params["code"] = code
        return r["redirect_uri"] + ("&" if "?" in r["redirect_uri"] else "?") + urlencode(params)

    def _chat(self, client_name: str) -> int:
        role = self.be.budgets.role("chat")
        now = self.db.now()
        return self.db.ex("INSERT INTO chats(title, provider, tier, created_at, updated_at) VALUES(?,?,?,?,?)",
                          (f"{client_name} (connector)"[:80], role.provider, role.tier, now, now))

    # ── tokens ─────────────────────────────────────────────────────────────
    def _issue(self, grant_id: int) -> dict:
        now = utcnow()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db.tx() as db:
            db.ex("INSERT INTO oauth_tokens(token_hash, grant_id, kind, expires_at) VALUES(?,?,?,?)", (_hash(access), grant_id, "access", iso(now + ACCESS_TTL)))
            db.ex("INSERT INTO oauth_tokens(token_hash, grant_id, kind, expires_at) VALUES(?,?,?,?)", (_hash(refresh), grant_id, "refresh", iso(now + REFRESH_TTL)))
        scopes = self.db.q1("SELECT scopes FROM oauth_grants WHERE id=?", (grant_id,))["scopes"]
        return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()), "refresh_token": refresh, "scope": scopes}

    def token(self, form: dict) -> dict:
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            row = self.db.q1("SELECT c.*, g.client_id, g.revoked_at FROM oauth_codes c JOIN oauth_grants g ON g.id=c.grant_id WHERE c.code_hash=?",
                             (_hash(form.get("code") or ""),))
            if not row or row["used_at"] or row["revoked_at"] or parse(row["expires_at"]) < utcnow():  # type: ignore[operator]
                if row and row["used_at"]:
                    self._revoke_grant(row["grant_id"])  # a replayed code: revoke what it issued
                raise OAuthError("invalid_grant", "The code is unknown, used or expired.")
            if row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri"):
                raise OAuthError("invalid_grant", "client_id or redirect_uri doesn't match the authorization request.")
            if not secrets.compare_digest(_s256(form.get("code_verifier") or ""), row["challenge"]):
                raise OAuthError("invalid_grant", "PKCE verification failed.")
            self.db.ex("UPDATE oauth_codes SET used_at=? WHERE code_hash=?", (self.db.now(), row["code_hash"]))
            return self._issue(row["grant_id"])
        if grant_type == "refresh_token":
            row = self._token(form.get("refresh_token"), "refresh")
            if row is None:
                raise OAuthError("invalid_grant", "The refresh token is unknown, revoked or expired.")
            if row["client_id"] != form.get("client_id"):
                raise OAuthError("invalid_grant", "client_id doesn't match.")
            self.db.ex("UPDATE oauth_tokens SET revoked_at=? WHERE token_hash=?", (self.db.now(), row["token_hash"]))
            return self._issue(row["grant_id"])
        raise OAuthError("unsupported_grant_type", "grant_type is authorization_code or refresh_token.")

    def _token(self, token: str | None, kind: str) -> dict | None:
        if not token:
            return None
        row = self.db.q1("SELECT t.*, g.client_id, g.scopes, g.chat_id, g.revoked_at AS grant_revoked FROM oauth_tokens t "
                         "JOIN oauth_grants g ON g.id=t.grant_id WHERE t.token_hash=? AND t.kind=?", (_hash(token), kind))
        if not row or row["revoked_at"] or row["grant_revoked"] or parse(row["expires_at"]) < utcnow():  # type: ignore[operator]
            return None
        return row

    def revoke_token(self, token: str | None) -> None:
        """RFC 7009: revoking either token revokes the grant (the client disconnects)."""
        for kind in ("access", "refresh"):
            row = self._token(token, kind)
            if row:
                self._revoke_grant(row["grant_id"])

    def _revoke_grant(self, grant_id: int) -> None:
        now = self.db.now()
        with self.db.tx() as db:
            db.ex("UPDATE oauth_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (now, grant_id))
            db.ex("UPDATE oauth_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE grant_id=?", (now, grant_id))

    def authenticate(self, authorization: str | None) -> dict:
        scheme, _, token = (authorization or "").partition(" ")
        row = self._token(token.strip(), "access") if scheme.lower() == "bearer" else None
        if row is None:
            raise Unauthorized("invalid_token")
        self.db.ex("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (self.db.now(), row["grant_id"]))
        return row

    # ── the owner's view ───────────────────────────────────────────────────
    def apps(self) -> list[dict]:
        rows = self.db.q("SELECT g.id, g.scopes, g.chat_id, g.created_at, g.last_used_at, c.name FROM oauth_grants g "
                         "JOIN oauth_clients c ON c.client_id=g.client_id WHERE g.revoked_at IS NULL ORDER BY g.id DESC")
        return [{"id": r["id"], "name": r["name"], "scopes": r["scopes"].split(), "chat_id": r["chat_id"], "created_at": r["created_at"],
                 "last_used_at": r["last_used_at"]} for r in rows]

    def revoke_app(self, grant_id: int) -> None:
        if not self.db.q1("SELECT 1 FROM oauth_grants WHERE id=? AND revoked_at IS NULL", (grant_id,)):
            raise NotFound(f"connected app {grant_id}")
        self._revoke_grant(grant_id)

    # ── /mcp ───────────────────────────────────────────────────────────────
    def _allowed(self, grant: dict) -> set[str]:
        kinds: set[str] = set()
        for sc in grant["scopes"].split():
            kinds |= SCOPE_TOOLS.get(sc, set())
        return kinds

    def handle(self, grant: dict, msg: Any, base_url: str) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if rid is None:
            return None
        if method == "initialize":
            return _ok(rid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                             "serverInfo": {"name": "l0wagent", "version": __version__}, "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(rid, {})
        allowed = self._allowed(grant)
        if method == "tools/list":
            return _ok(rid, {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema(),
                                        "annotations": {"readOnlyHint": t.kind == "read"}} for t in T.TOOLS if t.kind in allowed]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = T.BY_NAME.get(name) if isinstance(name, str) else None
            if tool is None or tool.kind not in allowed:
                return _ok(rid, _text(f"No tool named {name} is available with this connection's scopes.", True))
            return _ok(rid, self.call(grant, tool, args, base_url))
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def call(self, grant: dict, tool: Any, args: Any, base_url: str) -> dict:
        client = self.db.q1("SELECT name FROM oauth_clients WHERE client_id=?", (grant["client_id"],))
        ctx = T.Ctx(be=self.be, client=f"connector:{client['name'] if client else 'MCP client'}", base_url=base_url)
        text, is_error = T.run(ctx, tool.name, args if isinstance(args, dict) else {})
        chats = self.be.chats_
        chat_id = grant["chat_id"]
        if chat_id:
            chats._add(chat_id, "tool", "", {"call_id": f"mcp-{secrets.token_hex(4)}", "name": tool.name, "input": args, "result": text, "is_error": is_error})
            for card in ctx.cards:
                chats._add(chat_id, "card", "", card)
            chats._touch(chat_id)
            if ctx.cards:
                text += f" The card is waiting in the owner's chat “{client['name'] if client else 'connector'} (connector)”: {base_url}/c/{chat_id}"
        return _text(text, is_error)


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def check_scopes(scopes: list[str] | None) -> list[str] | None:
    if scopes is None:
        return None
    bad = [x for x in scopes if x not in SCOPES]
    if bad:
        raise BadRequest("bad_scope", f"Unknown scope: {' '.join(bad)}.")
    return scopes
