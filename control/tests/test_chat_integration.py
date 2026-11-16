# ruff: noqa: F811
import json

import httpx

from l0w_control import tools as T

from .test_chat import be, c, ok, settled  # noqa: F401
from .test_integrations import Upstream

TOKEN = "ghp_integrationtoken0123456789"


def _card(c, be):
    be.integrations.transport = httpx.MockTransport(Upstream())
    chat = ok(c.post("/api/chats", json={"message": "fake:integration connect github"}), 201)
    msgs = settled(c, chat["id"])
    card = next(m for m in msgs if m["role"] == "card")
    return chat, card


def test_the_card_carries_the_steps(c, be):
    _, card = _card(c, be)
    d = card["data"]
    assert d["kind"] == "integration" and d["title"] == "GitHub" and d["secret_name"] == "GITHUB_TOKEN"
    assert d["token_url"].startswith("https://github.com/") and "fine-grained" in d["guidance"]


def test_approving_saves_the_token_and_connects(c, be):
    chat, card = _card(c, be)
    url = f"/api/chats/{chat['id']}/cards/{card['id']}"
    assert c.post(url, json={"decision": "approve"}).status_code == 400
    done = ok(c.post(url, json={"decision": "approve", "value": TOKEN}))
    assert done["data"]["state"] == "done" and done["data"]["status"] == "ok" and done["data"]["tool_count"] == 3
    secret = next(x for x in ok(c.get("/api/secrets")) if x["name"] == "GITHUB_TOKEN")
    assert secret["scope"] == "integrations"
    assert ok(c.get("/api/integrations/github"))["status"] == "ok"
    everything = json.dumps(ok(c.get(f"/api/chats/{chat['id']}/messages")))
    assert TOKEN not in everything and "Connected GitHub: 3 tools (1 read-only)" in everything


def test_a_saved_secret_needs_no_value_and_a_failure_is_reported(c, be):
    be.integrations.transport = httpx.MockTransport(Upstream(status=401))
    ok(c.put("/api/secrets/GITHUB_TOKEN", json={"value": TOKEN, "scope": "integrations"}))
    chat = ok(c.post("/api/chats", json={"message": "fake:integration"}), 201)
    card = next(m for m in settled(c, chat["id"]) if m["role"] == "card")
    done = ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "approve"}))
    assert done["data"]["status"] == "error" and done["data"]["last_error"]
    msgs = json.dumps(ok(c.get(f"/api/chats/{chat['id']}/messages")))
    assert "Saved the token, but GitHub answered" in msgs and TOKEN not in msgs


def test_decline_and_validation(c, be):
    chat, card = _card(c, be)
    assert ok(c.post(f"/api/chats/{chat['id']}/cards/{card['id']}", json={"decision": "decline"}))["data"]["state"] == "declined"
    assert not be.list_integrations()
    ctx = T.Ctx(be=be)
    assert T.run(ctx, "propose_integration", {"preset": "nope"})[1]
    assert T.run(ctx, "propose_integration", {"name": "Bad Name", "url": "https://x.example", "secret_name": "X_TOKEN"})[1]
    assert not T.run(ctx, "propose_integration", {"name": "home-assistant", "title": "Home Assistant", "url": "https://ha.example/mcp", "secret_name": "ha_token"})[1]
    assert ctx.cards[0]["secret_name"] == "HA_TOKEN"
