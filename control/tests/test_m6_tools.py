"""The Network API against the real egress proxy (local stack, no Docker)."""

import httpx
import pytest

from .stack import TOKENS, Stack


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("m6net")).up()
    s.login()
    yield s
    s.down()


def rules(stack: Stack) -> list[dict]:
    return stack.client.get("/api/network").json()["rules"]


def test_network_rules_end_to_end(stack: Stack):
    # 1. The owner adds an allow rule and it is listed.
    r = stack.client.post("/api/network/rules", json={"pattern": "example.org", "action": "allow", "note": "docs"})
    assert 200 <= r.status_code < 300, r.text
    rule = r.json()
    listed = [x for x in rules(stack) if x["pattern"] == "example.org"]
    assert [x["id"] for x in listed] == [rule["id"]]
    assert listed[0]["action"] == "allow" and listed[0]["note"] == "docs"

    # 2. Blocking a model provider is refused with egress's own message.
    r = stack.client.post("/api/network/rules", json={"pattern": "api.anthropic.com", "action": "block"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "egress_refused" and body["detail"]
    assert not any(x["pattern"] == "api.anthropic.com" for x in rules(stack))

    # 3. A rule from an approved agent permission carries the permission's number.
    p = stack.client.post("/api/agent/permissions", headers={"X-Agent-Token": TOKENS["agent"]},
                          json={"text": "Read the weather", "actions": [{"kind": "egress_rule", "pattern": "api.weather.example"}]}).json()
    assert stack.client.post(f"/api/permissions/{p['id']}/decide", json={"decision": "allow", "scope": "always"}).status_code == 200
    weather = [x for x in rules(stack) if x["pattern"] == "api.weather.example"]
    assert len(weather) == 1 and weather[0]["note"] == f"permission #{p['id']}"

    # 4. A request through the proxy to a non-allowed local address is blocked and logged.
    try:
        resp = httpx.get("http://127.0.0.1:9/", proxy=f"http://127.0.0.1:{stack.ports['proxy']}", timeout=10)
        assert resp.status_code >= 400
    except httpx.HTTPError:
        pass
    blocked = stack.wait_for(lambda: stack.client.get("/api/network", params={"filter": "blocked"}).json()["requests"], what="a blocked request")
    assert any(q["host"] == "127.0.0.1" and q["action"] == "blocked" for q in blocked)

    # 5. Deleting the rule removes it.
    assert stack.client.delete(f"/api/network/rules/{rule['id']}").status_code == 200
    assert not any(x["id"] == rule["id"] for x in rules(stack))
