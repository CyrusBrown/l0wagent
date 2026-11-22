"""H5's done-when, in CI and with no Docker: two instances on two fake hubs become friends with a code, their assistants trade
messages with files, a message to an offline instance arrives from the gateway's outbox when it returns, and a pinned key that no
longer matches the directory raises the alarm.

Each instance is the whole chain from test_hub_join.py: its own fake hub (dev/hub), gateway (gateway/), tunnel service (tunnel/) and
control, each a subprocess. The hubs are two sites (`hub-a.localhost`, `hub-b.localhost`, so the addresses differ), and every
hub-bound connection of the gateways goes to the right hub's port with the real Host header (`L0W_HUB_CONNECT_TO` as a list of
`domain=host:port`), since `*.localhost` names may not resolve here. The outbox's first retry is one second
(`GATEWAY_OUTBOX_MIN_DELAY`) and the hub's offline page says `Retry-After: 1` (`FAKEHUB_OFFLINE_RETRY_AFTER`; the outbox honors
it), so the offline step takes seconds. Control tells the tunnel from the other local callers by its address (`[::1]`, see
HubStack(tunnel_from_ipv6)). Alice's assistant is the fake claude, run by the real runner, running the real `assistant send --file`."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import time

import httpx
import pytest
from l0wproto import keys

from l0w_control.auth import totp_code

from .stack import GW_CONTROL, HubStack

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to run the hub, gateway and tunnel projects")

PNG_LIKE = bytes(range(256)) * 40  # binary, 10 KiB: base64url's whole alphabet is in play
REPORT = b"pump results\n1,2,3\n"


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.fixture
def pair(tmp_path):
    if not _ipv6_loopback():
        pytest.skip("needs the IPv6 loopback (::1): it is how control tells the hub tunnel from the other local callers")
    ports = {}
    stacks = {}
    for who, host in (("alice", "hub-a.localhost"), ("bob", "hub-b.localhost")):
        (tmp_path / who).mkdir()
        stacks[who] = HubStack(tmp_path / who, hub_host=host, tunnel_from_ipv6=True, hub_env={"FAKEHUB_OFFLINE_RETRY_AFTER": "1"}, steps=3, delay=0.2, usage_policy="allow")
        ports[host] = stacks[who].hub_port
    reach = ",".join(f"{h}=127.0.0.1:{p}" for h, p in ports.items())
    for st in stacks.values():
        st.gateway_env = {"L0W_HUB_ALLOW_HTTP": "1", "L0W_HUB_CONNECT_TO": reach, "GATEWAY_OUTBOX_MIN_DELAY": "1", "GATEWAY_OUTBOX_INTERVAL": "0.5"}
    try:
        stacks["alice"].up(runner=True)
        stacks["bob"].up()
        yield stacks["alice"], stacks["bob"]
    finally:
        for st in stacks.values():
            st.down()


def join(st: HubStack, name: str) -> str:
    st.login()
    c = st.client
    secret = c.post("/api/account/totp/setup").json()["secret"]
    assert c.post("/api/account/totp/enable", json={"code": totp_code(secret, int(time.time()) // 30)}).status_code == 200
    r = c.post("/api/membership/join", json={"invite": st.invite(), "name": name, "backup": "made", "idempotency_key": f"join-{name}"})
    assert r.status_code == 200, r.text
    m = r.json()["membership"]
    assert m["state"] == "joined" and m["tunnel"]["state"] == "connected", m
    if not m["check"]["ok"]:
        inst = m["hostnames"]["instance"]
        pytest.fail(f"{m['check']}\n served: {st.visit(inst, '/.well-known/l0wagent/self').text[:1500]}\n local: {st.client.get('/.well-known/l0wagent/self').text[:1500]}")
    return m["address"]


def thread_items(st: HubStack, tid: int) -> list[dict]:
    return [i["message"] for i in st.client.get(f"/api/threads/{tid}/items").json()["items"] if i.get("message")]


def friend(st: HubStack, address: str) -> dict | None:
    return next((f for f in st.client.get("/api/friends").json() if f["address"] == address), None)


def gateway_outbox(st: HubStack) -> list[dict]:
    r = httpx.get(f"http://127.0.0.1:{st.gw_port}/v0/peer/outbox", headers={"Authorization": f"Bearer {GW_CONTROL}"}, timeout=10)
    return r.json()["entries"]


def test_two_instances_on_two_hubs_become_friends_and_trade_messages_with_files(pair):
    alice, bob = pair
    a_addr, b_addr = join(alice, "alice"), join(bob, "bob")
    assert a_addr == "alice@hub-a.localhost" and b_addr == "bob@hub-b.localhost"

    # ── friends, with a code ───────────────────────────────────────────────
    code = alice.client.post("/api/friends/codes", json={"label": "Bob", "idempotency_key": "code-1"}).json()
    assert code["code"].startswith("l0wf1:") and code["address"] == a_addr
    r = bob.client.post("/api/friends/requests", json={"code": code["code"], "note": "Hi, it's Bob", "idempotency_key": "req-1"})
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "sent", json.dumps([r.json()["error"], [{k: v for k, v in e.items() if k != "doc"} for e in gateway_outbox(bob)]])
    assert r.json()["address"] == a_addr and r.json()["fingerprint"] == code["fingerprint"]
    incoming = alice.wait_for(lambda: [q for q in alice.client.get("/api/friends/requests").json() if q["direction"] == "in" and q["state"] == "pending"],
                              timeout=30, what="the request on alice's side")
    assert incoming[0]["address"] == b_addr and incoming[0]["code_used"] and incoming[0]["note"] == "Hi, it's Bob"
    r = alice.client.post(f"/api/friends/requests/{incoming[0]['id']}/approve")
    assert r.status_code == 200, r.text
    for st, other in ((alice, b_addr), (bob, a_addr)):
        f = st.wait_for(lambda st=st, other=other: (f := friend(st, other)) and f["state"] == "friends" and f, timeout=30, what=f"friends with {other}")
        assert f["thread_id"] and f["share"] == "nothing" and not f["key_alarm"]
    fp_a = alice.client.get("/api/friends/fingerprint").json()["fingerprint"]
    assert friend(bob, a_addr)["fingerprint"] == fp_a == code["fingerprint"]  # what Bob pinned is what Alice's own page shows
    a_thread, b_thread = friend(alice, b_addr)["thread_id"], friend(bob, a_addr)["thread_id"]

    # ── Alice's assistant writes with a file ───────────────────────────────
    (alice.tmp / "work" / "report.bin").write_bytes(PNG_LIKE)
    (alice.tmp / "work" / "report.txt").write_bytes(REPORT)
    assert alice.client.put("/api/instructions", json={"text": "fake:friends fake:sendfile /work/report.txt fake:sendfile /work/report.bin"}).status_code == 200
    run_id = alice.client.post("/api/runs", json={"budget": 15}).json()["id"]
    done = alice.wait_for(lambda: (r := alice.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="alice's run to end")
    assert done["status"] == "done", done
    sent = next(i for i in alice.timeline(run_id) if i["kind"] == "tool" and "send --thread" in i["text"])
    assert "with 2 files" in (sent["data"] or {}).get("result", ""), sent
    mine = [m for m in thread_items(alice, a_thread) if m["sender"] == "assistant"]
    assert len(mine) == 1 and [x["name"] for x in mine[0]["attachments"]] == ["report.txt", "report.bin"]

    got = bob.wait_for(lambda: [m for m in thread_items(bob, b_thread) if m["sender"] == "peer"], timeout=30, what="the message on bob's side")
    assert len(got) == 1 and "nothing about their projects" in got[0]["body"] and got[0]["via"] == "peer"
    assert sorted(a["name"] for a in got[0]["attachments"]) == ["report.bin", "report.txt"]
    inbox = bob.tmp / "work" / "_inbox" / "peers" / a_addr
    assert (inbox / "report.txt").read_bytes() == REPORT and (inbox / "report.bin").read_bytes() == PNG_LIKE
    assert all(a["path"].startswith(f"_inbox/peers/{a_addr}/") for a in got[0]["attachments"])
    assert sorted(p.name for p in inbox.iterdir()) == ["report.bin", "report.txt"]

    # ── Bob replies, with an upload; a name that's taken gets a number ─────
    up = bob.client.post("/api/uploads", files={"file": ("report.txt", b"thanks, here is mine", "text/plain")}, data={"idempotency_key": "up-1"})
    assert up.status_code == 201, up.text
    r = bob.client.post(f"/api/threads/{b_thread}/messages", json={"body": "Got it, thanks!", "upload_ids": [up.json()["id"]], "idempotency_key": "reply-1"})
    assert r.status_code == 201, r.text
    back = alice.wait_for(lambda: [m for m in thread_items(alice, a_thread) if m["sender"] == "peer"], timeout=30, what="the reply on alice's side")
    assert back[0]["body"] == "Got it, thanks!" and [a["name"] for a in back[0]["attachments"]] == ["report.txt"]
    a_inbox = alice.tmp / "work" / "_inbox" / "peers" / b_addr
    assert (a_inbox / "report.txt").read_bytes() == b"thanks, here is mine"
    # The same name again lands beside it, never over it.
    up2 = bob.client.post("/api/uploads", files={"file": ("report.txt", b"second", "text/plain")}, data={"idempotency_key": "up-2"}).json()
    bob.client.post(f"/api/threads/{b_thread}/messages", json={"body": "and again", "upload_ids": [up2["id"]], "idempotency_key": "reply-2"})
    alice.wait_for(lambda: (a_inbox / "report.txt").exists() and len(list(a_inbox.iterdir())) == 2, timeout=30, what="the second file")
    assert (a_inbox / "report.txt").read_bytes() == b"thanks, here is mine" and (a_inbox / "report-2.txt").read_bytes() == b"second"

    # ── Files: over 5 MiB is refused before anything goes out; exactly 5 MiB goes through all three base64 layers intact ──
    big = alice.tmp / "work" / "big.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024 + 1))
    agent = {"X-Agent-Token": "agent-token-test"}
    r = httpx.post(f"{alice.url}/api/agent/messages", headers=agent, json={"thread": a_thread, "body": "x", "files": ["/work/big.bin"]})
    assert r.status_code == 400 and r.json()["error"] == "files_too_large", r.text
    upbig = alice.client.post("/api/uploads", files={"file": ("big.bin", big.read_bytes(), "application/octet-stream")}, data={"idempotency_key": "up-big"}).json()
    r = alice.client.post(f"/api/threads/{a_thread}/messages", json={"body": "x", "upload_ids": [upbig["id"]], "idempotency_key": "big-1"})
    assert r.status_code == 400 and r.json()["error"] == "files_too_large", r.text
    assert len([m for m in thread_items(alice, a_thread) if m["sender"] in ("assistant", "owner")]) == 1  # nothing was stored for the refused ones
    exact = os.urandom(5 * 1024 * 1024)
    (alice.tmp / "work" / "ok.bin").write_bytes(exact)
    r = httpx.post(f"{alice.url}/api/agent/messages", headers=agent, json={"thread": a_thread, "body": "the big one", "files": ["/work/ok.bin"]})
    assert r.status_code == 200, r.text
    bob.wait_for(lambda: (inbox / "ok.bin").exists() and (inbox / "ok.bin").stat().st_size == len(exact), timeout=60, what="the 5 MiB file")
    assert hashlib.sha256((inbox / "ok.bin").read_bytes()).digest() == hashlib.sha256(exact).digest()

    # ── Bob's tunnel is down: Alice's message waits in her gateway's outbox, then arrives ──
    bob.kill("tunnel")
    bob.wait_for(lambda: hub_says_offline(bob), timeout=30, what="bob's hub to see the tunnel gone")
    n_before = len([m for m in thread_items(bob, b_thread) if m["sender"] == "peer"])
    r = alice.client.post(f"/api/threads/{a_thread}/messages", json={"body": "are you there?", "idempotency_key": "late-1"})
    assert r.status_code == 201, r.text
    entry = alice.wait_for(lambda: next((e for e in gateway_outbox(alice) if e["kind"] == "message" and e["state"] == "queued" and e["attempts"] >= 1), None),
                           timeout=30, what="the message queued in alice's gateway outbox")
    assert thread_items(alice, a_thread)[-1]["delivery"] == "queued"
    time.sleep(1.5)  # a retry or two fail while he's away
    assert len([m for m in thread_items(bob, b_thread) if m["sender"] == "peer"]) == n_before
    bob.start_tunnel()
    late = bob.wait_for(lambda: [m for m in thread_items(bob, b_thread) if m["sender"] == "peer" and m["body"] == "are you there?"], timeout=45, what="the late message")
    assert len(late) == 1
    assert bob.wait_for(lambda: next(e for e in gateway_outbox(alice) if e["id"] == entry["id"])["state"] == "delivered", timeout=15, what="delivered in the outbox")

    # ── A key that no longer matches the directory is an alarm, never accepted ──
    # Alice's housekeeping (the scheduler) reads the outbox back and checks friends' keys when it starts. Her gateway's pin of Bob
    # is replaced with another key, which is what a re-registration of Bob with new keys and no rotation looks like to her.
    alice.kill("gateway")
    pins = alice.tmp / "gateway" / "peers.json"
    data = json.loads(pins.read_text())
    data[b_addr]["sign_key"] = keys.generate("sign").key.to_dict()
    pins.write_text(json.dumps(data))
    alice.start_gateway()
    alice.kill("control")
    alice.control_env["L0W_SCHEDULER"] = "1"
    alice.start_control()
    alice.login()
    f = alice.wait_for(lambda: (f := friend(alice, b_addr)) and f["key_alarm"] and f, timeout=60, what="the key alarm on alice's side")
    assert f["state"] == "friends" and "no rotation" in f["key_alarm"]
    assert thread_items(alice, a_thread)[-1]["delivery"] == "delivered"  # housekeeping read the outbox back
    con = sqlite3.connect(alice.tmp / "control" / "control.db", timeout=10)
    assert con.execute("SELECT COUNT(*) FROM notifications WHERE title LIKE '%key changed%'").fetchone()[0] >= 1
    con.close()


def hub_says_offline(st: HubStack) -> bool:
    inst = st.client.get("/api/membership").json()["hostnames"]["instance"]
    return st.visit(inst, "/api/meta").status_code == 502
