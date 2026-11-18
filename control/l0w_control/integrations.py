"""Brokered MCP integrations: control holds the credential and forwards the agent's tool calls.

- The owner adds an integration (a remote MCP server reached over streamable HTTP, plus the name
  of a secret sent as a bearer token). Control lists its tools; each gets a policy: allowed, ask
  (a permission card each time) or blocked. Every tool starts on ask, except a preset's own vetted
  read-only tools (a server's `readOnlyHint` is only shown, never trusted). A tool whose description
  or schema later changes goes back to ask and is flagged.
- The URL is vetted (safefetch): https, never loopback or this install's own services; http on a
  private network only when the owner ticks "this is on my network".
- The agent reaches each integration as an MCP server at /api/agent/mcp/<name> with its agent
  token. It sees only tools that aren't blocked; the credential never enters the sandbox.
- An "ask" call files a permission with one `mcp_call` action and returns "asked as permission #N".
  Allowing it once lets that exact call (same tool, same arguments) through one time; allowing it
  always sets the tool's policy to allowed.
- Every call is logged with redacted, clipped arguments. Results are redacted before the agent sees them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__
from .backends.base import BadRequest, Conflict, NotFound
from .db import Database
from .safefetch import PinnedTransport, vet_url

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
POLICIES = ("allowed", "ask", "blocked")
PROTOCOL = "2025-06-18"
ARGS_LOG_MAX = 2000
RESULT_MAX = 200_000
TIMEOUT = 60.0

PRESETS: dict[str, dict[str, Any]] = {
    "github": {
        "title": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth_secret": "GITHUB_TOKEN",
        "token_url": "https://github.com/settings/personal-access-tokens/new",
        "guidance": ("Create a fine-grained personal access token limited to the repositories the assistant should see. "
                     "Read-only is enough for reading code, issues and pull requests: Contents, Issues, Pull requests and Metadata "
                     "set to Read. Add Read and write only for what it should change. Save it as GITHUB_TOKEN with the scope "
                     "\"Integrations only\" so it never enters the agent's environment."),
        "egress": [],  # control calls GitHub's MCP server itself; the sandbox needs no rule for it
        "read_only_tools": [
            "get_me", "get_issue", "list_issues", "search_issues", "get_issue_comments", "get_pull_request", "list_pull_requests",
            "search_pull_requests", "get_pull_request_files", "get_pull_request_diff", "get_pull_request_comments",
            "get_pull_request_reviews", "get_pull_request_status", "get_file_contents", "search_code", "search_repositories",
            "search_users", "list_commits", "get_commit", "list_branches", "list_tags", "get_tag", "list_releases",
            "get_latest_release", "list_notifications", "get_notification_details", "list_workflows", "list_workflow_runs",
            "get_workflow_run", "list_code_scanning_alerts", "get_code_scanning_alert", "list_secret_scanning_alerts",
            "get_secret_scanning_alert", "list_gists", "get_gist",
        ],
    },
}


class UpstreamError(Exception):
    pass


def spec_hash(description: str, schema: dict) -> str:
    return hashlib.sha256(json.dumps([description, schema], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class McpClient:
    """A minimal streamable-HTTP MCP client: one short session per use (initialize, then calls)."""

    def __init__(self, url: str, token: str | None, transport: httpx.BaseTransport | None = None, timeout: float = TIMEOUT):
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "User-Agent": f"l0wagent/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.url, self.http = url, httpx.Client(headers=headers, timeout=timeout, transport=transport, follow_redirects=False)
        self.session: str | None = None
        self.n = 0

    def __enter__(self) -> McpClient:
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "l0wagent", "version": __version__}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *exc: object) -> None:
        if self.session:
            try:
                self.http.delete(self.url, headers={"Mcp-Session-Id": self.session})
            except httpx.HTTPError:
                pass
        self.http.close()

    def _post(self, body: dict) -> httpx.Response:
        headers = {"Mcp-Session-Id": self.session} if self.session else {}
        if self.session:
            headers["MCP-Protocol-Version"] = PROTOCOL
        try:
            r = self.http.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise UpstreamError(f"Couldn't reach {self.url}: {e.__class__.__name__}") from None
        if r.status_code in (401, 403):
            raise UpstreamError(f"The server refused the credentials (HTTP {r.status_code}).")
        if r.status_code >= 400:
            raise UpstreamError(f"The server answered HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        rid = self.n
        r = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if r.headers.get("mcp-session-id"):
            self.session = r.headers["mcp-session-id"]
        msg = _read_response(r, rid)
        if "error" in msg:
            err = msg["error"] or {}
            raise UpstreamError(f"{method} failed: {err.get('message', err)}")
        return msg.get("result") or {}

    def tools(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(20):
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            out += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def call(self, tool: str, args: dict) -> dict:
        return self._rpc("tools/call", {"name": tool, "arguments": args})


def _read_response(r: httpx.Response, rid: int) -> dict:
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("text/event-stream"):
        for block in r.text.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == rid:
                return msg
        raise UpstreamError("The server's event stream ended without an answer.")
    try:
        msg = r.json()
    except ValueError:
        raise UpstreamError("The server's answer wasn't JSON.") from None
    if isinstance(msg, list):
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == rid), {})
    if not isinstance(msg, dict):
        raise UpstreamError("The server's answer wasn't a JSON-RPC message.")
    return msg


def _j(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


class Integrations:
    def __init__(self, db: Database, vault, file_permission: Callable[..., Any], transport: httpx.BaseTransport | None = None):
        self.db, self.vault, self.file_permission, self.transport = db, vault, file_permission, transport
        self.extra_presets: Callable[[], dict[str, dict[str, Any]]] = dict  # enabled extensions' presets
        self.timeout: Callable[[], float] = lambda: TIMEOUT                    # INTEGRATION_TIMEOUT_S, set by the backend

    def all_presets(self) -> dict[str, dict[str, Any]]:
        return {**self.extra_presets(), **PRESETS}

    # ── owner side ─────────────────────────────────────────────────────────
    def _row(self, name: str) -> dict:
        row = self.db.q1("SELECT * FROM integrations WHERE name=?", ((name or "").strip().lower(),))
        if not row:
            raise NotFound(f"integration {name}")
        return row

    def view(self, row: dict) -> dict:
        tools = self.db.q("SELECT name, description, read_only, policy, present, changed_at FROM integration_tools WHERE integration_id=? ORDER BY present DESC, name", (row["id"],))
        calls = self.db.q("SELECT tool, run_id, outcome, permission_id, detail, at FROM integration_calls WHERE integration_id=? ORDER BY id DESC LIMIT 20", (row["id"],))
        secret_ok = bool(row["auth_secret"]) and self.vault.exists(row["auth_secret"])
        return {"name": row["name"], "title": row["title"], "preset": row["preset"], "url": row["url"], "auth_secret": row["auth_secret"],
                "secret_saved": secret_ok, "enabled": bool(row["enabled"]), "status": row["status"], "last_error": row["last_error"],
                "synced_at": row["synced_at"], "created_at": row["created_at"],
                "allow_private": bool(row["allow_private"]),
                "tools": [{**{k: v for k, v in t.items() if k != "changed_at"}, "read_only": bool(t["read_only"]), "present": bool(t["present"]),
                           "changed_since_allowed": bool(t["changed_at"])} for t in tools],
                "recent_calls": calls}

    def listing(self) -> list[dict]:
        return [self.view(r) for r in self.db.q("SELECT * FROM integrations ORDER BY title")]

    def get(self, name: str) -> dict:
        return self.view(self._row(name))

    def presets(self) -> list[dict]:
        return [{"id": k, **{f: x for f, x in v.items() if f != "extension"}} for k, v in self.all_presets().items()]

    def create(self, name: str, title: str | None, preset: str | None, url: str | None, auth_secret: str | None, allow_private: bool = False) -> dict:
        p = self.all_presets().get(preset or "") if preset else {}
        if preset and not p:
            raise BadRequest("bad_preset", f"Unknown preset {preset!r}.")
        name = (name or preset or "").strip().lower()
        if not NAME_RE.match(name):
            raise BadRequest("bad_name", "Integration names look like github or home-assistant: lowercase letters, digits and -, 2–32 characters.")
        if self.db.q1("SELECT 1 FROM integrations WHERE name=?", (name,)):
            raise Conflict("exists", f"An integration named {name} already exists.")
        url = _check_url(url or p.get("url", ""), allow_private)
        secret = (auth_secret or p.get("auth_secret") or "").strip().upper() or None
        if secret:
            from .vault import validate_name
            secret = validate_name(secret)
        now = self.db.now()
        self.db.ex("INSERT INTO integrations(name, title, preset, url, auth_secret, allow_private, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (name, (title or p.get("title") or name).strip()[:80], preset, url, secret, int(bool(allow_private)), now, now))
        if not secret or self.vault.exists(secret):
            self.sync(name, quiet=True)
        return self.get(name)

    def update(self, name: str, title: str | None = None, url: str | None = None, auth_secret: str | None = None, enabled: bool | None = None,
               allow_private: bool | None = None) -> dict:
        row = self._row(name)
        fields: dict[str, Any] = {}
        private = bool(row["allow_private"]) if allow_private is None else allow_private
        if allow_private is not None:
            fields["allow_private"] = int(allow_private)
        if title is not None:
            fields["title"] = title.strip()[:80] or row["name"]
        if url is not None:
            fields["url"] = _check_url(url, private)
        elif allow_private is not None:
            _check_url(row["url"], private)
        if auth_secret is not None:
            from .vault import validate_name
            fields["auth_secret"] = validate_name(auth_secret) if auth_secret.strip() else None
        if enabled is not None:
            fields["enabled"] = int(enabled)
        if fields:
            fields["updated_at"] = self.db.now()
            self.db.ex(f"UPDATE integrations SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), row["id"]))
        return self.get(name)

    def delete(self, name: str) -> None:
        row = self._row(name)
        with self.db.tx() as db:
            db.ex("DELETE FROM integration_calls WHERE integration_id=?", (row["id"],))
            db.ex("DELETE FROM integration_tools WHERE integration_id=?", (row["id"],))
            db.ex("DELETE FROM integrations WHERE id=?", (row["id"],))

    def set_policy(self, name: str, tool: str, policy: str) -> dict:
        row = self._row(name)
        if policy not in POLICIES:
            raise BadRequest("bad_policy", "A tool's policy is allowed, ask or blocked.")
        if not self.db.ex("UPDATE integration_tools SET policy=?, changed_at=NULL WHERE integration_id=? AND name=?", (policy, row["id"], tool)):
            raise NotFound(f"tool {tool}")
        return self.get(name)

    def _client(self, row: dict) -> McpClient:
        token = None
        if row["auth_secret"]:
            token = self.vault.value_for_control(row["auth_secret"])
            if token is None:
                raise UpstreamError(f"The secret {row['auth_secret']} isn't saved yet.")
        return McpClient(row["url"], token, self.transport or PinnedTransport(bool(row["allow_private"])), float(self.timeout()))

    def sync(self, name: str, quiet: bool = False) -> dict:
        """List the upstream's tools. New tools get the default policy; existing policies are kept."""
        row = self._row(name)
        now = self.db.now()
        try:
            with self._client(row) as mcp:
                tools = mcp.tools()
        except UpstreamError as e:
            self.db.ex("UPDATE integrations SET status='error', last_error=?, updated_at=? WHERE id=?", (self.vault.redact(str(e)), now, row["id"]))
            if quiet:
                return self.get(name)
            raise Conflict("upstream", self.vault.redact(str(e)) or "") from None
        seen = set()
        with self.db.tx() as db:
            for t in tools:
                tname = str(t.get("name") or "")[:128]
                if not tname or tname in seen:
                    continue
                seen.add(tname)
                ro = bool((t.get("annotations") or {}).get("readOnlyHint"))
                schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object"}
                desc = str(t.get("description") or "")[:2000]
                digest = spec_hash(desc, schema)
                vetted = tname in (self.all_presets().get(row["preset"] or "") or {}).get("read_only_tools", ())
                old = db.q1("SELECT policy, spec_hash FROM integration_tools WHERE integration_id=? AND name=?", (row["id"], tname))
                if old is None:
                    db.ex("INSERT INTO integration_tools(integration_id, name, description, input_schema_json, read_only, policy, spec_hash) VALUES(?,?,?,?,?,?,?)",
                          (row["id"], tname, desc, json.dumps(schema), int(ro), "allowed" if vetted else "ask", digest))
                    continue
                # A changed description or schema is a different tool: back to ask until the owner decides again.
                changed = old["spec_hash"] is not None and old["spec_hash"] != digest
                reset = changed and old["policy"] == "allowed"
                db.ex("UPDATE integration_tools SET description=?, input_schema_json=?, read_only=?, present=1, spec_hash=?, "
                      "policy=CASE WHEN ? THEN 'ask' ELSE policy END, changed_at=CASE WHEN ? THEN ? ELSE changed_at END WHERE integration_id=? AND name=?",
                      (desc, json.dumps(schema), int(ro), digest, int(reset), int(changed), now, row["id"], tname))
            db.ex(f"UPDATE integration_tools SET present=0 WHERE integration_id=? AND name NOT IN ({','.join('?' * len(seen)) or 'NULL'})", (row["id"], *seen))
            db.ex("UPDATE integrations SET status='ok', last_error=NULL, synced_at=?, updated_at=? WHERE id=?", (now, now, row["id"]))
        return self.get(name)

    # ── agent side (MCP over /api/agent/mcp/<name>) ────────────────────────
    def handle(self, name: str, msg: Any, run_id: int | None) -> dict | None:
        """One JSON-RPC message from the agent. Returns the response, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _err(None, -32600, "Invalid request")
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if rid is None:
            return None
        row = self.db.q1("SELECT * FROM integrations WHERE name=? AND enabled=1", (name,))
        if not row:
            return _err(rid, -32001, f"No integration named {name} is enabled.")
        if method == "initialize":
            return _ok(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                             "serverInfo": {"name": f"l0w-{name}", "version": __version__},
                             "instructions": (f"{row['title']}, brokered by control: the credential stays with control. Some tools ask the "
                                              "owner first; such a call returns 'asked as permission #N'. Once it's allowed, make the same "
                                              "call again with the same arguments.")})
        if method == "ping":
            return _ok(rid, {})
        if method == "tools/list":
            tools = self.db.q("SELECT name, description, input_schema_json, read_only, policy FROM integration_tools "
                              "WHERE integration_id=? AND present=1 AND policy!='blocked' ORDER BY name", (row["id"],))
            return _ok(rid, {"tools": [{"name": t["name"],
                                        "description": ("[Asks the owner first.] " if t["policy"] == "ask" else "") + t["description"],
                                        "inputSchema": _j(t["input_schema_json"], {"type": "object"}),
                                        "annotations": {"readOnlyHint": bool(t["read_only"])}} for t in tools]})
        if method == "tools/call":
            tool, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(tool, str) or not isinstance(args, dict):
                return _err(rid, -32602, "tools/call needs a name and an arguments object")
            return _ok(rid, self.call(row, tool, args, run_id))
        return _err(rid, -32601, f"Method not found: {method}")

    def _log(self, row: dict, tool: str, args: dict, run_id: int | None, outcome: str, permission_id: int | None = None, detail: str | None = None) -> None:
        self.db.ex("INSERT INTO integration_calls(integration_id, tool, run_id, args_json, outcome, permission_id, detail, at) VALUES(?,?,?,?,?,?,?,?)",
                   (row["id"], tool, run_id, (self.vault.redact(json.dumps(args)) or "")[:ARGS_LOG_MAX], outcome, permission_id,
                    (self.vault.redact(detail) or None) if detail else None, self.db.now()))

    def call(self, row: dict, tool: str, args: dict, run_id: int | None) -> dict:
        t = self.db.q1("SELECT policy, present FROM integration_tools WHERE integration_id=? AND name=?", (row["id"], tool))
        if not t or not t["present"] or t["policy"] == "blocked":
            self._log(row, tool, args, run_id, "blocked")
            return _text(f"{tool} isn't available: the owner hasn't allowed it on {row['title']}.", error=True)
        if t["policy"] == "ask":
            grant = self._grant(row["name"], tool, args)
            if grant is None:
                pending = self._pending(row["name"], tool, args)
                if pending is not None:
                    return _text(f"Still waiting: this call was asked as permission #{pending}. Make it again once the owner allows it.", error=True)
                summary = json.dumps(args, sort_keys=True)
                p = self.file_permission(run_id, None, f"Call {row['title']} → {tool}", None,
                                         [{"kind": "mcp_call", "integration": row["name"], "tool": tool, "args": args}], trusted=True)
                self._log(row, tool, args, run_id, "asked", p.id)
                return _text(f"Asked as permission #{p.id}: the owner must allow {tool} with these arguments ({summary[:300]}). "
                             "Wait for the answer (it arrives like any other), then make exactly the same call again.", error=True)
            self.db.ex("UPDATE permission_actions SET result_json=? WHERE id=?", (json.dumps({"used_at": self.db.now(), "run_id": run_id}), grant))
        try:
            with self._client(row) as mcp:
                res = mcp.call(tool, args)
        except UpstreamError as e:
            self._log(row, tool, args, run_id, "error", detail=str(e))
            return _text(f"{row['title']} failed: {self.vault.redact(str(e))}", error=True)
        out = json.loads(self.vault.redact(json.dumps(res)) or "{}")
        if len(json.dumps(out)) > RESULT_MAX:
            out = _text(f"The result was too large ({len(json.dumps(out))} bytes); ask for less.", error=True)
        self._log(row, tool, args, run_id, "error" if out.get("isError") else "ok")
        return out

    def _actions(self, integration: str, tool: str, args: dict, status: str) -> list[dict]:
        want = args_hash(tool, args)
        rows = self.db.q("SELECT a.id, a.payload_json, a.result_json, p.id AS pid, p.scope FROM permission_actions a JOIN permissions p ON p.id=a.permission_id "
                         "WHERE a.kind='mcp_call' AND p.status=? ORDER BY a.id", (status,))
        return [r for r in rows if (pl := _j(r["payload_json"], {})).get("integration") == integration and pl.get("hash") == want]

    def _grant(self, integration: str, tool: str, args: dict) -> int | None:
        """An unused one-time approval for exactly this call (its action id)."""
        for r in self._actions(integration, tool, args, "allowed"):
            if r["scope"] == "once" and not _j(r["result_json"], {}).get("used_at"):
                return r["id"]
        return None

    def _pending(self, integration: str, tool: str, args: dict) -> int | None:
        rows = self._actions(integration, tool, args, "open")
        return rows[0]["pid"] if rows else None

    def apply_always(self, payload: dict) -> dict:
        """An 'always' approval: the tool becomes allowed."""
        row = self._row(payload["integration"])
        self.db.ex("UPDATE integration_tools SET policy='allowed', changed_at=NULL WHERE integration_id=? AND name=?", (row["id"], payload["tool"]))
        return {"policy": "allowed"}

    def check_action(self, a: dict) -> tuple[str, dict]:
        """Validate an mcp_call action (filed by control itself, never by the agent's /permissions)."""
        row = self._row(str(a.get("integration", "")))
        tool, args = str(a.get("tool", "")), a.get("args") if isinstance(a.get("args"), dict) else {}
        shown = json.loads(self.vault.redact(json.dumps(args)) or "{}")
        return "mcp_call", {"integration": row["name"], "title": row["title"], "tool": tool, "args": shown, "hash": args_hash(tool, args)}


def _check_url(url: str, allow_private: bool = False) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+(/\S*)?$", url):
        raise BadRequest("bad_url", "The server's URL starts with https:// (or http:// on your own network).")
    vet_url(url, allow_private)
    return url


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}
