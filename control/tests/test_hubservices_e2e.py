"""H6's path end to end, in CI and with no Docker: an instance joins a fake hub that runs the test service `echo`
(`--echo --echo-tester alice`), the service connects by itself (hello, welcome with a `tester` role grant), the assistant calls it
(`assistant hubnet call`, run by the fake claude through the real runner), a role-gated method works only with `--role tester`,
and when the owner switches the service off the hub's side forgets the instance and calls are refused.

The chain is the one in test_friends_e2e.py: the fake hub (dev/hub), the gateway, the tunnel service and control, each a subprocess;
envelopes between the instance and the service travel as any visitor's would (the service answers by the hub's own routing, through
the tunnel to control's inbox). Control tells the tunnel from the other local callers by its address (`[::1]`)."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3

import httpx
import pytest

from .stack import HubStack
from .test_friends_e2e import join

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to run the hub, gateway and tunnel projects")

AGENT = {"X-Agent-Token": "agent-token-test"}


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.fixture
def stack(tmp_path):
    if not _ipv6_loopback():
        pytest.skip("needs the IPv6 loopback (::1): it is how control tells the hub tunnel from the other local callers")
    st = HubStack(tmp_path, hub_host="hub.localhost", tunnel_from_ipv6=True, steps=3, delay=0.2, usage_policy="allow",
                  hub_env={"FAKEHUB_ECHO": "1", "FAKEHUB_ECHO_TESTERS": "alice"})
    st.gateway_env = {"L0W_HUB_ALLOW_HTTP": "1", "L0W_HUB_CONNECT_TO": f"127.0.0.1:{st.hub_port}", "GATEWAY_OUTBOX_MIN_DELAY": "1", "GATEWAY_OUTBOX_INTERVAL": "0.5"}
    try:
        st.up(runner=True)
        yield st
    finally:
        st.down()


def service(st: HubStack, address: str) -> dict | None:
    return next((x for x in st.client.get("/api/membership/services").json() if x["address"] == address), None)


def agent_call(st: HubStack, **body) -> httpx.Response:
    return httpx.post(f"{st.url}/api/agent/hubnet/call", headers=AGENT, json=body, timeout=30)


def test_join_connects_echo_the_agent_calls_it_and_switching_it_off_ends_it(stack):
    st = stack
    address = join(st, "alice")
    echo = f"echo@{address.split('@')[1]}"

    # ── joining connects the service by itself: hello out, welcome back with a tester grant ──
    svc = st.wait_for(lambda: (s := service(st, echo)) and s["state"] == "connected" and s, timeout=40, what="echo to connect")
    assert svc["kind"] == "custom" and svc["title"] == "Echo" and svc["enabled"] and svc["default_on"]
    assert svc["roles"] == ["tester"] and svc["roles_held"] == ["tester"]  # alice is a tester; the grant itself isn't in the API
    assert "payload" not in json.dumps(svc)

    # ── the agent: what it can see, and its calls, through the real CLI in a real run ──
    listing = httpx.get(f"{st.url}/api/agent/hubnet", headers=AGENT).json()
    assert [(x["address"], x["roles"]) for x in listing["services"]] == [(echo, ["tester"])]
    assert "grant" not in json.dumps(listing).lower() and "sigs" not in json.dumps(listing)
    assert st.client.put("/api/instructions", json={"text": "fake:hubnet fake:hubrole tester"}).status_code == 200
    run_id = st.client.post("/api/runs", json={"budget": 15}).json()["id"]
    done = st.wait_for(lambda: (r := st.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="the run to end")
    assert done["status"] == "done", done
    tools = {i["text"]: (i["data"] or {}).get("result", "") for i in st.timeline(run_id) if i["kind"] == "tool"}

    def result_of(fragment: str) -> list[str]:
        return [r for t, r in tools.items() if fragment in t]

    [ls] = result_of("hubnet ls")
    assert echo in ls and "roles held: tester" in ls and "sigs" not in ls
    [echoed] = result_of("hubnet call echo echo")
    assert "untrusted content from hub service" in echoed and '"x": 1' in echoed
    [who] = result_of("hubnet call echo whoami")
    assert address in who and '"tester"' in who  # the service honored the grant control attached
    plain, roled = result_of("hubnet call echo secret")[0], result_of("--role tester")[-1]
    assert "not_allowed" in plain and "xyzzy" not in plain  # no role: refused by the service itself
    assert "xyzzy" in roled  # --role tester: control attached the grant, and the service honored it

    # ── direct calls with the agent token (what the CLI does), and what control refuses ──
    r = agent_call(st, service="echo", method="echo", args={"y": [1, 2]})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["result"] == {"y": [1, 2]} and "untrusted" in r.json()["untrusted"]
    r = agent_call(st, service="echo", method="secret", role="admin")
    assert r.status_code == 400 and "no_role" in r.json()["detail"]  # a role it doesn't hold: nothing sent
    r = agent_call(st, service=echo, method="nope")
    assert r.json()["ok"] is False and r.json()["error"]["code"] == "unknown_method" and r.json()["error"]["from_service"] is True

    # every call is logged with who made it, and nothing in the log carries a grant
    con = sqlite3.connect(st.tmp / "control" / "control.db", timeout=10)
    log = con.execute("SELECT method, role, by, state FROM service_calls ORDER BY created_at, rowid").fetchall()
    assert ("secret", "tester", "agent", "ok") in log and ("secret", None, "agent", "error") in log and ("echo", None, "agent", "ok") in log
    assert not con.execute("SELECT 1 FROM service_calls WHERE args_json LIKE '%role-grant%' OR result_json LIKE '%role-grant%'").fetchall()
    con.close()

    # ── the owner switches echo off: the service forgets us, and calls are refused ──
    off = st.client.patch(f"/api/membership/services/{echo}", json={"enabled": False})
    assert off.status_code == 200 and off.json()["state"] == "off" and off.json()["roles_held"] == []

    def hub_members() -> dict:
        state = json.loads((st.tmp / "hub" / "state.json").read_text())
        return state.get("echo", {}).get("members", {})

    st.wait_for(lambda: address not in hub_members(), timeout=30, what="echo to forget the instance after the goodbye")
    r = agent_call(st, service="echo", method="echo")
    assert r.status_code == 409 and "not_connected" in r.json()["detail"]
    assert httpx.get(f"{st.url}/api/agent/hubnet", headers=AGENT).json()["services"] == []

    # ── and on again: a fresh hello, a fresh welcome, the grant is back ──
    on = st.client.patch(f"/api/membership/services/{echo}", json={"enabled": True})
    assert on.status_code == 200
    st.wait_for(lambda: (s := service(st, echo)) and s["state"] == "connected" and s["roles_held"] == ["tester"], timeout=40, what="echo to connect again")
    r = agent_call(st, service="echo", method="secret", role="tester")
    assert r.json()["ok"] is True and r.json()["result"] == {"secret": "xyzzy"}
