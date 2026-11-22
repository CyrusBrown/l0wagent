"""H5 part 2 end to end on the local stack: the real `assistant` CLI, run by the fake claude's friends scenario, against a running control.
There's no hub here (no gateway answers), so this covers what needs none: reading friends, answering one in its peer thread (kept
for sending), and drafting a friend request for the owner."""

import json
import sqlite3

import pytest

from .stack import Stack

BOB = "bob@other.example"
ANA = "ana@hub.example"


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path, steps=3, delay=0.2, control_env={"L0W_GATEWAY_URL": "http://127.0.0.1:9", "L0W_GATEWAY_TOKEN": "gateway-token-test"}).up()
    s.login()
    yield s
    s.down()


def seed(stack: Stack) -> int:
    """A joined instance, one friend with their peer thread, and a message from them waiting for the agent."""
    con = sqlite3.connect(stack.tmp / "control" / "control.db", timeout=10)
    now = "2026-11-22T10:00:00Z"
    con.execute("INSERT INTO hub_membership(id, state, hub_base, hub_domain, hub_name, hub_key, capabilities, api, name, address, mode, hostnames, tunnel_url, joined_at)"
                " VALUES(1,'joined','https://hub.example','hub.example','Hub','{}','{}','x','mara','mara@hub.example','tunnel',?,'wss://t',?)",
                (json.dumps({"instance": "mara.hub.example", "files": "files--mara.hub.example"}), now))
    tid = con.execute("INSERT INTO threads(project_id, title, type, created_by, created_at, updated_at) VALUES(NULL,?,'peer','owner',?,?)", (BOB, now, now)).lastrowid
    con.execute("INSERT INTO friends(address, display_name, fingerprint, state, share, thread_id, presence_json, presence_at, created_at, updated_at) "
                "VALUES(?,'Bobby','7Q2M-9KXA-4D1F-R0VE','friends','shared',?,?,?,?,?)",
                (BOB, tid, json.dumps({"state": "asleep", "next": "tonight", "now": None}), now, now, now))
    con.execute("INSERT INTO messages(thread_id, sender, body, via, created_at) VALUES(?,'peer','Are the pump results ready?','peer',?)", (tid, now))
    con.commit()
    con.close()
    return tid


def test_the_assistant_reads_its_friends_answers_one_and_drafts_a_request(stack: Stack):
    c = stack.client
    tid = seed(stack)
    assert c.put("/api/instructions", json={"text": f"fake:friends fake:befriend {ANA}"}).status_code == 200
    run_id = c.post("/api/runs", json={"budget": 15}).json()["id"]
    done = stack.wait_for(lambda: (r := stack.run(run_id))["status"] not in ("starting", "running") and r, timeout=90, what="run to end")
    assert done["status"] == "done", done
    tools = {i["text"]: (i["data"] or {}).get("result", "") for i in stack.timeline(run_id) if i["kind"] == "tool"}
    listing = next(v for k, v in tools.items() if k.endswith("assistant friends"))
    assert BOB in listing and f"thread #{tid}" in listing and "projects you share with them" in listing and "asleep, wakes tonight" in listing
    sent = next(v for k, v in tools.items() if "send --thread" in k)
    assert sent.startswith("sent (message #"), sent
    drafted = next(v for k, v in tools.items() if "friends request" in k)
    assert "drafted" in drafted and ANA in drafted, drafted

    # The reply is in the peer thread and kept for sending (no gateway to take it here); the draft waits for the owner.
    items = c.get(f"/api/threads/{tid}/items").json()["items"]
    mine = [i["message"] for i in items if i["message"]["sender"] == "assistant"]
    assert len(mine) == 1 and "nothing about their projects" in mine[0]["body"] and mine[0]["delivery"] == "queued"
    req = next(r for r in c.get("/api/friends/requests").json() if r["address"] == ANA)
    assert req["made_by"] == "assistant" and req["state"] == "pending"
    # The friend's message reached the agent (in the brief, with the owner's sharing rule) and is marked delivered.
    con = sqlite3.connect(stack.tmp / "control" / "control.db", timeout=10)
    assert con.execute("SELECT delivered_at FROM messages WHERE sender='peer'").fetchone()[0]
    con.close()
