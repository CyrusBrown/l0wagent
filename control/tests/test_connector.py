"""The MCP connector through the real app: discovery, registration, OAuth with PKCE, /mcp, grants."""

import base64
import hashlib
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from l0w_control.auth import iso, utcnow
from tests.test_conversations import be, c  # noqa: F401

CB = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64


def challenge(v=VERIFIER):
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def register(cl, uri=CB):  # noqa: F811
    r = cl.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [uri]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def params(cid, scope="read write", uri=CB, **extra):
    p = {"response_type": "code", "client_id": cid, "redirect_uri": uri, "code_challenge": challenge(),
         "code_challenge_method": "S256", "scope": scope, "state": "xyz"}
    p.update(extra)
    return p


def get_code(cl, cid, scope="read write", scopes=None):
    p = params(cid, scope)
    r = cl.get("/oauth/authorize", params=p, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/connect?")
    q = dict((k, v[0]) for k, v in parse_qs(urlparse(r.headers["location"]).query).items())
    info = cl.get("/api/connector/request", params=q)
    assert info.status_code == 200, info.text
    body = {"params": q, "approve": True}
    if scopes is not None:
        body["scopes"] = scopes
    r = cl.post("/api/connector/consent", json=body)
    assert r.status_code == 200, r.text
    qs = parse_qs(urlparse(r.json()["redirect"]).query)
    assert qs["state"] == ["xyz"]
    return qs["code"][0]


def exchange(cl, cid, code, verifier=VERIFIER, uri=CB):
    return cl.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": uri,
                                        "client_id": cid, "code_verifier": verifier})


def connect(cl, scope="read write"):
    cid = register(cl)
    r = exchange(cl, cid, get_code(cl, cid, scope))
    assert r.status_code == 200, r.text
    return cid, r.json()


def rpc(cl, tok, method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        body["id"] = id
    if params is not None:
        body["params"] = params
    return cl.post("/mcp", json=body, headers={"Authorization": f"Bearer {tok}"})


def call(cl, tok, name, args=None):
    r = rpc(cl, tok, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_discovery(c):  # noqa: F811
    pr = c.get("/.well-known/oauth-protected-resource").json()
    assert pr["resource"].endswith("/mcp")
    assert pr["authorization_servers"]
    a = c.get("/.well-known/oauth-authorization-server").json()
    assert a["code_challenge_methods_supported"] == ["S256"]
    assert a["registration_endpoint"].endswith("/oauth/register")
    assert a["token_endpoint"].endswith("/oauth/token")
    assert a["authorization_endpoint"].endswith("/oauth/authorize")


def test_mcp_needs_token(c):  # noqa: F811
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith('Bearer resource_metadata="') and www.endswith('/.well-known/oauth-protected-resource"')
    assert rpc(c, "nonsense", "ping").status_code == 401


def test_register(c):  # noqa: F811
    r = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [CB]})
    assert r.status_code == 201 and r.json()["client_id"]
    r = c.post("/oauth/register", json={"client_name": "Evil", "redirect_uris": ["http://evil.example/cb"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    assert c.post("/oauth/register", json={"client_name": "Local", "redirect_uris": ["http://127.0.0.1:3333/cb"]}).status_code == 201


def test_full_flow_and_authorize_errors(c):  # noqa: F811
    cid = register(c)
    p = params(cid)
    q = dict(p)
    info = c.get("/api/connector/request", params=q).json()
    assert info["client_name"] == "Claude"
    assert [x["scope"] for x in info["scopes"]] == ["read", "write"]
    code = get_code(c, cid)
    # wrong verifier is refused (and doesn't burn a good exchange afterwards)
    r = exchange(c, cid, code, verifier="w" * 64)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = exchange(c, cid, code)
    assert r.status_code == 200
    t = r.json()
    assert t["scope"] == "read write" and t["access_token"] and t["refresh_token"] and t["token_type"] == "Bearer"
    # authorize errors: no redirect
    bad = params(cid)
    del bad["code_challenge"]
    r = c.get("/oauth/authorize", params=bad, follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers
    r = c.get("/oauth/authorize", params=params(cid, uri="https://other.example/cb"), follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


def test_code_reuse_revokes_grant(c):  # noqa: F811
    cid = register(c)
    code = get_code(c, cid)
    t = exchange(c, cid, code).json()
    assert rpc(c, t["access_token"], "ping").status_code == 200
    r = exchange(c, cid, code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert rpc(c, t["access_token"], "ping").status_code == 401
    assert c.get("/api/connected-apps").json() == []


def test_mcp_tools(c):  # noqa: F811
    _, t = connect(c)
    tok = t["access_token"]
    r = rpc(c, tok, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert r["serverInfo"]["name"] == "l0wagent" and r["instructions"]
    assert rpc(c, tok, "notifications/initialized", id=None).status_code == 202
    names = {x["name"] for x in rpc(c, tok, "tools/list").json()["result"]["tools"]}
    assert {"list_projects", "create_thread", "propose_wakeup"} <= names
    assert "reply_now" not in names
    res = call(c, tok, "list_projects")
    assert not res["isError"]
    res = call(c, tok, "reply_now", {"thread_id": 1})
    assert res["isError"] and "No tool named" in res["content"][0]["text"] and "available" in res["content"][0]["text"]


def test_read_only_scope_hides_write_tools(c):  # noqa: F811
    cid = register(c)
    t = exchange(c, cid, get_code(c, cid, "read write", scopes=["read"])).json()
    assert t["scope"] == "read"
    names = {x["name"] for x in rpc(c, t["access_token"], "tools/list").json()["result"]["tools"]}
    assert "list_projects" in names and "create_thread" not in names and "propose_wakeup" not in names


def test_calls_logged_in_chat(c):  # noqa: F811
    _, t = connect(c)
    tok = t["access_token"]
    call(c, tok, "list_projects")
    apps = c.get("/api/connected-apps").json()
    assert len(apps) == 1 and apps[0]["name"] == "Claude" and apps[0]["scopes"] == ["read", "write"]
    chat_id = apps[0]["chat_id"]
    assert apps[0]["last_used_at"]
    msgs = c.get(f"/api/chats/{chat_id}/messages").json()
    tools = [m for m in msgs if m["role"] == "tool"]
    assert tools and tools[0]["data"]["name"] == "list_projects"
    res = call(c, tok, "propose_wakeup", {"budget": 10, "reason": "check the build"})
    assert not res["isError"], res
    assert f"/c/{chat_id}" in res["content"][0]["text"]
    msgs = c.get(f"/api/chats/{chat_id}/messages").json()
    cards = [m for m in msgs if m["role"] == "card"]
    assert len(cards) == 1 and cards[0]["data"]["kind"] == "wakeup"


def test_refresh_rotates(c):  # noqa: F811
    cid, t = connect(c)

    def refresh(rt):
        return c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid})

    r = refresh(t["refresh_token"])
    assert r.status_code == 200
    n = r.json()
    assert n["access_token"] != t["access_token"] and n["refresh_token"] != t["refresh_token"]
    assert rpc(c, n["access_token"], "ping").status_code == 200
    # Presented again at once: a client retrying a lost response, so it gets a fresh pair and the grant lives.
    retry = refresh(t["refresh_token"])
    assert retry.status_code == 200 and retry.json()["refresh_token"] not in (t["refresh_token"], n["refresh_token"])
    assert refresh(n["refresh_token"]).status_code == 200


def _age_rotation(be, seconds):  # noqa: F811
    be.db.ex("UPDATE oauth_tokens SET revoked_at=? WHERE revoked_at IS NOT NULL", (iso(utcnow() - timedelta(seconds=seconds)),))


def test_reused_refresh_token_revokes_the_grant(c, be):  # noqa: F811
    cid, t = connect(c)

    def refresh(rt):
        return c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid})

    n = refresh(t["refresh_token"]).json()
    _age_rotation(be, 60)
    r = refresh(t["refresh_token"])  # long after rotation: theft
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # Everything the grant issued is dead, including the newest tokens.
    assert refresh(n["refresh_token"]).status_code == 400
    assert rpc(c, n["access_token"], "ping").status_code == 401
    assert c.get("/api/connected-apps").json() == []


def test_registration_prunes_grantless_clients_and_is_rate_limited(c, be, monkeypatch):  # noqa: F811
    old = register(c)
    be.db.ex("UPDATE oauth_clients SET created_at=? WHERE client_id=?", (iso(utcnow() - timedelta(days=2)), old))
    keep_cid, _ = connect(c)
    be.db.ex("UPDATE oauth_clients SET created_at=? WHERE client_id=?", (iso(utcnow() - timedelta(days=2)), keep_cid))
    register(c)
    ids = {r["client_id"] for r in be.db.q("SELECT client_id FROM oauth_clients")}
    assert old not in ids and keep_cid in ids  # old and grantless: gone; old with a grant: kept
    # At the cap the oldest grantless ones go.
    monkeypatch.setattr("l0w_control.connector.MAX_CLIENTS", 3)
    first = register(c)
    register(c)
    register(c)
    ids = {r["client_id"] for r in be.db.q("SELECT client_id FROM oauth_clients")}
    assert len(ids) == 3 and keep_cid in ids and first not in ids
    # Ten registrations an hour per address.
    monkeypatch.setattr("l0w_control.connector.MAX_CLIENTS", 100)
    r = None
    for _ in range(12):
        r = c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [CB]})
    assert r.status_code == 429 and r.json()["error"] == "slow_down"


def test_deny(c):  # noqa: F811
    cid = register(c)
    r = c.post("/api/connector/consent", json={"params": params(cid), "approve": False})
    assert r.status_code == 200
    qs = parse_qs(urlparse(r.json()["redirect"]).query)
    assert qs["error"] == ["access_denied"] and qs["state"] == ["xyz"] and "code" not in qs
    assert c.get("/api/connected-apps").json() == []


def test_revoke_app(c):  # noqa: F811
    cid, t = connect(c)
    app = c.get("/api/connected-apps").json()[0]
    assert c.delete(f"/api/connected-apps/{app['id']}").status_code == 200
    assert rpc(c, t["access_token"], "ping").status_code == 401
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert c.get("/api/connected-apps").json() == []
    assert c.delete(f"/api/connected-apps/{app['id']}").status_code == 404


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_oauth_revoke(c, which):  # noqa: F811
    cid, t = connect(c)
    assert c.post("/oauth/revoke", data={"token": t[which]}).status_code == 200
    assert rpc(c, t["access_token"], "ping").status_code == 401
    assert c.get("/api/connected-apps").json() == []


def test_expired_access_token(c, be):  # noqa: F811
    _, t = connect(c)
    assert rpc(c, t["access_token"], "ping").status_code == 200
    h = hashlib.sha256(t["access_token"].encode()).hexdigest()
    be.db.ex("UPDATE oauth_tokens SET expires_at=? WHERE token_hash=?", ("2000-01-01T00:00:00+00:00", h))
    assert rpc(c, t["access_token"], "ping").status_code == 401


def test_bad_json_is_parse_error(c):  # noqa: F811
    _, t = connect(c)
    r = c.post("/mcp", content=b"{nope", headers={"Authorization": f"Bearer {t['access_token']}", "content-type": "application/json"})
    assert r.status_code == 400 and json.loads(r.text)["error"]["code"] == -32700


def test_skill_zip(c):  # noqa: F811
    import io
    import zipfile
    r = c.get("/connector/skill.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    text = zipfile.ZipFile(io.BytesIO(r.content)).read("l0wagent/SKILL.md").decode()
    assert text.startswith("---\nname: l0wagent") and "can't change the harness" in text


def test_pages_refuse_framing(c):  # noqa: F811
    r = c.get("/api/meta")
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in r.headers["content-security-policy"]
