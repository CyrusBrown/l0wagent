"""Redaction: encoded forms, the schema check on SCRUB, tool results, agent notes, delivery lines, once-approvals."""

# ruff: noqa: F811, F402
import base64
import json
from urllib.parse import quote

import pytest

from l0w_control import tools as T
from l0w_control.api import schemas as s
from l0w_control.vault import SCRUB, forms, store_text
from tests.test_connector import call, connect
from tests.test_conversations import AGENT, be, c, ok  # noqa: F401, F811

SECRET = "sk-live/abc+123=XYZ_9876"


@pytest.fixture(autouse=True)
def _work_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("L0W_WORK_DIR", str(tmp_path / "work"))  # read before the backend is built by the `be` fixture


def agent(c, method, path, **kw):
    return c.request(method, "/api/agent" + path, headers=AGENT, **kw)


def test_redact_encoded_forms(be):
    be.vault.set("MY_KEY", SECRET)
    raw = SECRET.encode()
    variants = [SECRET, base64.b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip("="), base64.urlsafe_b64encode(raw).decode(),
                base64.urlsafe_b64encode(raw).decode().rstrip("="), quote(SECRET, safe=""), json.dumps(SECRET)[1:-1]]
    for v in variants:
        out = be.vault.redact(f"token={v}; end")
        assert out == "token=[secret:MY_KEY]; end", v
    assert SECRET in forms(SECRET)


def test_short_values_are_not_matched_in_any_form(be):
    be.vault.set("SHORT", "abc1234")  # 7 characters: never redacted
    assert be.vault.redact("abc1234 YWJjMTIzNA==") == "abc1234 YWJjMTIzNA=="


def test_store_text_takes_a_vault_or_a_backend(be):
    be.vault.set("MY_KEY", SECRET)
    assert store_text(be, f"x {SECRET}") == store_text(be.vault, f"x {SECRET}") == "x [secret:MY_KEY]"
    assert store_text(be, None) is None


# Columns that can't hold owner or agent text. Everything else of type TEXT must be in vault.SCRUB.
NOT_TEXT = {
    "extensions": {"name", "version", "sdk", "source", "found_at"}, "extension_kv": {"ext", "key"}, "extension_cards": {"ext", "kind", "status"},
    # ids, tokens, hashes and keys
    "api_tokens": {"id", "token_hash", "scopes_json"}, "auth_sessions": {"id", "kind", "token_hash", "ip", "user_agent"},
    "oauth_codes": {"code_hash", "redirect_uri", "challenge", "resource"}, "oauth_tokens": {"token_hash", "kind"},
    "oauth_grants": {"client_id", "scopes"}, "oauth_clients": {"client_id", "redirect_uris"}, "recovery_codes": {"code_hash"},
    "idempotency_keys": {"key", "endpoint", "response_json"}, "shelf_versions": {"blob_path", "sha256"},
    "passkeys": {"credential_id", "public_key"}, "push_subscriptions": {"endpoint", "p256dh", "auth"},
    "users": {"username", "password_hash", "totp_secret", "timezone"}, "secrets": {"name", "scope"}, "login_attempts": {"ip", "username"},
    "change_log": {"entity", "op", "entity_id"}, "kv": {"key", "value"}, "settings": {"key", "value_json"},
    # file names, device and client labels: short names chosen by the browser, the OS or a service, not prose
    "attachments": {"name", "volume", "path", "mime"}, "uploads": {"name", "path", "mime", "sha256", "client_id"},
    "chat_files": {"name", "mime", "path"},
    # fixed vocabularies and control's own records: kinds, statuses, modes, providers, tiers, paths
    "messages": {"client_id", "sender", "via", "delivery"}, "chat_messages": {"idempotency_key", "role", "provider", "model"},
    "permission_actions": {"kind", "result_json"},  # result_json: previous setting values etc., written by control
    "integration_calls": {"tool", "outcome"}, "integration_tools": {"name", "policy", "description", "input_schema_json", "spec_hash"},
    "integrations": {"name", "title", "preset", "url", "auth_secret", "status"},  # the owner's connection config; the credential is a secret name
    "jobs": {"log_path", "status"}, "notifications": {"kind", "ref_kind", "ref_id", "url"}, "claims": {"status"},
    "permissions": {"status", "scope"}, "projects": {"slug", "status", "doc_path", "links_json", "created_by"},
    "promote_requests": {"status"}, "providers": {"id", "kind", "label", "config_json"},
    "questions": {"kind", "status", "scale_labels_json"}, "rate_limit_events": {"limit_type", "status"},
    "reviewer_messages": {"sender", "kind"}, "routines": {"builtin", "extension", "schedule_json", "provider", "tier", "status", "created_by"},
    "run_sessions": {"runtime", "session_id", "model", "effort", "log_path", "runner_session", "state"},
    "runs": {"kind", "why", "provider", "tier", "status", "end_reason", "workspace_head", "bucket", "reviewer_mode", "switch_tier", "level"},
    "secret_reads": {"how"}, "security_alerts": {"session_id", "status"},
    "shelf_items": {"volume", "path", "mode", "added_by", "last_sha256"}, "suggestions": {"kind", "target_kind", "status"},
    "tasks": {"status"}, "threads": {"type", "status", "created_by"}, "usage_ledger": {"provider", "who", "bucket"},
    "usage_snapshots": {"provider", "source", "error"}, "wakeups": {"provider", "tier", "kind", "created_by", "status"},
    "harness_news": {"date", "title", "body"}, "chats": {"provider", "tier", "status", "context_thread_ids"}, "timeline_items": {"kind"},
}
# Labels of devices and clients (not prose), by (table, column), so a new label column still has to be looked at.
LABELS = {("passkeys", "label"), ("auth_sessions", "label"), ("api_tokens", "label"), ("push_subscriptions", "label"), ("oauth_clients", "name"),
          ("users", "display_name")}
# Timestamps: any TEXT column named like these holds a time, not text.
TIME_COLS = {"at", "created_at", "updated_at", "delivered_at", "answered_at", "decided_at", "resolved_at", "started_at", "ended_at", "applied_at",
             "reverted_at", "revoked_at", "last_used_at", "last_seen_at", "expires_at", "confirmed_at", "password_changed_at", "last_read_at",
             "last_ok_at", "announced_at", "sent_at", "read_at", "synced_at", "archived_at", "last_run_at", "stop_requested_at", "deadline",
             "waiting_until", "expected_end", "resets_at", "fetched_at", "five_hour_resets_at", "seven_day_resets_at", "removed_at", "used_at", "changed_at", "hold_started_at"}


def test_every_text_column_is_scrubbed_or_explicitly_not_text(be):
    scrub = set(SCRUB)
    for t, c in SCRUB:  # the list itself must name real columns
        assert be.db.q(f"SELECT {c} FROM {t} LIMIT 0") == []
    missing = []
    for tbl in be.db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        t = tbl["name"]
        for col in be.db.q(f"PRAGMA table_info({t})"):
            name = col["name"]
            if "TEXT" not in (col["type"] or "").upper() or (t, name) in scrub or (t, name) in LABELS or name in TIME_COLS or name in NOT_TEXT.get(t, set()):
                continue
            missing.append(f"{t}.{name}")
    assert not missing, f"add to vault.SCRUB (owner/agent text) or to NOT_TEXT here (ids, statuses, hashes, keys): {missing}"


def test_scrub_covers_new_tables(be):
    cid = be.db.ex("INSERT INTO chats(title, provider, tier, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                   (f"about {SECRET}", "claude", "sonnet", "open", be.db.now(), be.db.now()))
    be.vault.set("MY_KEY", SECRET)
    assert SECRET not in be.db.q1("SELECT title FROM chats WHERE id=?", (cid,))["title"]


def test_tool_results_are_redacted_for_every_caller(c, be, tmp_path):
    ok(c.put("/api/secrets/MY_KEY", json={"value": SECRET}))
    (tmp_path / "work" / ".env").write_text(f"KEY={SECRET}\n")
    args = {"volume": "work", "path": ".env"}
    text, err = T.run(T.Ctx(be=be, chat_id=None, client="x"), "read_file", args)
    assert not err and SECRET not in text and "[secret:MY_KEY]" in text
    # the connector's /mcp
    _, tok = connect(c)
    res = call(c, tok["access_token"], "read_file", args)
    assert SECRET not in json.dumps(res) and "[secret:MY_KEY]" in res["content"][0]["text"]
    # the sidecar's MCP path
    chat = be.chats_.create(s.ChatCreate(message="hi", idempotency_key="k-redact-1"))
    reply = be.chats_.mcp(chat.id, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": args}})
    assert SECRET not in json.dumps(reply) and "[secret:MY_KEY]" in json.dumps(reply)


def test_agent_notes_status_and_description_are_redacted(c, be):
    ok(c.put("/api/secrets/MY_KEY", json={"value": SECRET}))
    run_id = be.db.ex("INSERT INTO runs(kind, why, provider, tier, status, started_at, deadline) VALUES('wakeup','test','claude','sonnet','running',?,?)",
                      (be.db.now(), be.db.now()))
    be.runs_mgr.active_run_id = lambda: run_id
    for path, body in (("/notes", {"text": f"found {SECRET}"}), ("/status", {"text": f"using {SECRET}"}), ("/run/describe", {"text": f"about {SECRET}"})):
        r = agent(c, "POST", path, json={**body, "run_id": run_id})
        assert r.status_code == 200, r.text
    row = be.db.q1("SELECT now_sentence, description FROM runs WHERE id=?", (run_id,))
    assert SECRET not in json.dumps(row) and "[secret:MY_KEY]" in row["now_sentence"] and "[secret:MY_KEY]" in row["description"]
    assert "[secret:MY_KEY]" in be.db.q1("SELECT text FROM run_notes WHERE run_id=?", (run_id,))["text"]


def test_new_secret_announcement_follows_scope(c, be):
    be.db.ex("INSERT INTO projects(slug, name, summary, status, created_by, created_at, updated_at) VALUES('garden','Garden','','active','owner',?,?)",
             (be.db.now(), be.db.now()))
    ok(c.put("/api/secrets/GITHUB_TOKEN", json={"value": "ghp_ABCDEFGHIJKLMNOP", "scope": "integrations"}))
    ok(c.put("/api/secrets/GARDEN_KEY", json={"value": "garden-key-12345678", "scope": "project:garden"}))
    text = ok(agent(c, "GET", "/inbox"))["text"]
    assert "GITHUB_TOKEN" not in text
    assert "New secret GARDEN_KEY" in text and "Only in runs on project Garden's threads" in text
    assert be.db.q1("SELECT announced_at FROM secrets WHERE name='GITHUB_TOKEN'")["announced_at"]


def test_delivery_line_names_how_the_message_was_posted(c, be):
    t = ok(c.post("/api/threads", json={"title": "Via", "first_message": "plain"}), 201)
    be.conv.post_owner_message(t["id"], "from an app", [], None, via="connector:ChatGPT")
    be.conv.post_owner_message(t["id"], "from a chat", [], None, via="chat:3", sender="chat")
    text = ok(agent(c, "GET", "/inbox"))["text"]
    assert "Owner, via ChatGPT (connector) (message #" in text
    assert "Owner (via chat) (message #" in text
    assert "- Owner (message #" in text


def test_once_approval_given_after_the_run_ended_is_undone_at_the_next_run_end(c, be):
    ok(c.put("/api/secrets/DUMMY_X", json={"value": "dummy-value-1234"}))  # keeps vault non-empty; not used
    base = be.settings.get("HOLD_MINUTES")
    p = ok(agent(c, "POST", "/permissions", json={"text": "Shorter holds", "actions": [{"kind": "setting", "key": "HOLD_MINUTES", "value": "2"}]}))
    # run A (the asking run) has ended already; the owner approves between runs
    ok(c.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "once"}))
    assert be.settings.get("HOLD_MINUTES") == 2
    # run B ends: the end-of-run revert (it doesn't depend on which run asked)
    assert be.conv.revert_once() == ["set HOLD_MINUTES to 2"]
    assert be.settings.get("HOLD_MINUTES") == base
    assert be.conv.revert_once() == []
