"""H5 part 1, control's side of friends and peer messages (friends.py), against a fake gateway.

The fake gateway answers the endpoints control calls (gateway/README.md); `/v0/peer/open` takes the
"envelope" as JSON describing what a real gateway would have verified and opened, so each test says
what arrived. The tunnel's caller class is a TestClient from the tunnel subnet."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from l0wproto import b64

from l0w_control.app import create_app
from l0w_control.auth import utcnow
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env
from l0w_control.membership import Gateway

PW = "correct horse battery"
TUNNEL_IP = ("10.255.254.9", 40000)
TUNNEL_NET = "10.255.254.0/24"
ME = "mara@hub.example"
OUR_FP = "WZCY-8FG5-JZ5E-V87N"
BOB = "bob@other.example"
BOB_FP = "7Q2M-9KXA-4D1F-R0VE"
TOKEN = {"v": "l0w/0.1", "type": "delivery-token", "payload": "x", "sigs": []}


@dataclass
class FakeGateway:
    """What control calls on the gateway, recorded; answers can be scripted."""

    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)
    errors: dict[str, tuple[int, str, str]] = field(default_factory=dict)  # path -> (status, error, detail), for the next call
    pins: dict[str, str] = field(default_factory=dict)  # address -> fingerprint (default BOB_FP)
    send_state: str = "queued"
    outbox: list[dict] = field(default_factory=list)
    down: bool = False
    n: int = 0

    def paths(self) -> list[str]:
        return [p for _, p, _ in self.calls]

    def bodies(self, path: str) -> list[dict]:
        return [b for _, p, b in self.calls if p == path and b is not None]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("gateway down", request=request)
        path = request.url.path
        raw = request.content
        body = json.loads(raw) if raw and path != "/v0/peer/open" else None
        self.calls.append((request.method, path, body if path != "/v0/peer/open" else None))
        if path in self.errors:
            status, error, detail = self.errors.pop(path)
            return httpx.Response(status, json={"error": error, "detail": detail})
        if path == "/v0/peer/open":
            opened = json.loads(raw)
            if "_error" in opened:
                status, error, detail = opened["_error"]
                return httpx.Response(status, json={"error": error, "detail": detail})
            return httpx.Response(200, json=opened)
        if path == "/v0/peer/fingerprint":
            return httpx.Response(200, json={"fingerprint": OUR_FP})
        if path == "/v0/peer/pin":
            fp = self.pins.get(body["address"], BOB_FP)
            return httpx.Response(200, json={"address": body["address"], "fingerprint": fp, "hostnames": {"instance": "x", "files": "y"}, "rotated": False})
        if path in ("/v0/peer/revoke", "/v0/peer/forget"):
            return httpx.Response(200, json={"ok": True})
        if path == "/v0/peer/send":
            self.n += 1
            return httpx.Response(200, json={"id": f"out-{self.n}", "state": self.send_state, "error": "their instance said no" if self.send_state == "failed" else None})
        if path == "/v0/peer/outbox":
            return httpx.Response(200, json={"entries": self.outbox})
        return httpx.Response(200, json={})


@dataclass
class World:
    be: SqliteBackend
    c: TestClient  # the owner, from the LAN
    t: TestClient  # the hub tunnel
    gw: FakeGateway
    inbox: object
    n: int = 0

    def arrive(self, sender: str, kind: str, body: dict, env_id: str | None = None, iat: str = "2026-11-22T10:00:00Z", fp: str = BOB_FP):
        """An envelope the gateway would open as this."""
        self.n += 1
        opened = {"from": sender, "from_kid": "k1.x", "fingerprint": fp, "id": env_id or f"env-{self.n}", "seq": self.n, "kind": kind, "iat": iat, "body": body}
        return self.t.post("/api/peer/v0/inbox", content=json.dumps(opened).encode())

    def befriend(self, address: str = BOB) -> int:
        now = self.be.db.now()
        self.be.db.ex("INSERT INTO friends(address, display_name, fingerprint, state, created_at, updated_at) VALUES(?,?,?,'friends',?,?)", (address, address, BOB_FP, now, now))
        return self.be.friends_._thread_for(address)

    def note(self, kind: str = "friends") -> list[dict]:
        return self.be.db.q("SELECT * FROM notifications WHERE kind=? ORDER BY id", (kind,))

    def run(self, coro):
        return self.be._await(coro)


@pytest.fixture
def w(tmp_path, monkeypatch):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    monkeypatch.setenv("L0W_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("L0W_TUNNEL_SUBNET", TUNNEL_NET)
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", tunnel_subnet=TUNNEL_NET,
              runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    be = SqliteBackend(env=env)
    gw = FakeGateway()
    be.membership_.gw = Gateway("http://gateway", "tok", transport=httpx.MockTransport(gw.handler))
    be.db.ex("INSERT INTO hub_membership(id, state, hub_base, hub_domain, hub_name, hub_key, capabilities, api, name, address, mode, hostnames, tunnel_url, joined_at)"
             " VALUES(1,'joined','https://hub.example','hub.example','Hub','{}','{}','x','mara',?,'tunnel',?,'wss://t','2026-11-18T00:00:00Z')",
             (ME, json.dumps({"instance": "mara.hub.example", "files": "files--mara.hub.example"})))
    app = create_app(be)
    with TestClient(app, client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        yield World(be, c, TestClient(app, client=TUNNEL_IP), gw, inbox)


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


# ── the whole story ────────────────────────────────────────────────────────
def test_request_out_accept_in_message_in_lands_in_the_peer_thread(w):
    r = ok(w.c.post("/api/friends/requests", json={"address": "BOB@other.example", "note": "hi, it's Mara", "idempotency_key": "k1"}), 201)
    assert r["state"] == "sent" and r["direction"] == "out" and r["address"] == BOB and r["fingerprint"] == BOB_FP
    assert w.gw.bodies("/v0/peer/pin")[0] == {"address": BOB}
    send = w.gw.bodies("/v0/peer/send")[0]
    assert send["kind"] == "friend-request" and send["request_token"] is True
    assert send["body"] == {"code": None, "note": "hi, it's Mara", "by": "owner"}
    assert [f["state"] for f in ok(w.c.get("/api/friends"))] == ["requested_out"]

    # Their acceptance arrives.
    assert ok(w.arrive(BOB, "friend-accept", {"request": "out-1", "token": TOKEN}), 202)["id"] == "env-1"
    f = ok(w.c.get("/api/friends"))[0]
    assert f["state"] == "friends" and f["thread_id"] and f["share"] == "nothing" and f["waking"] == "never" and f["presence_detail"] == "coarse"
    assert ok(w.c.get("/api/friends/requests"))[0]["state"] == "approved"
    thread = ok(w.c.get(f"/api/threads/{f['thread_id']}"))
    assert thread["type"] == "peer" and thread["title"] == BOB
    assert [n["title"] for n in w.note()] == ["Friend request accepted"]

    # A message with a file, and a hostile file name and a bad hash.
    data = b"pump relay: SRD-05VDC\n"
    good = {"name": "../../etc/notes.txt", "type": "text/plain", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "data": b64.encode(data)}
    bad = {"name": "evil.sh", "type": "text/x-sh", "size": 3, "sha256": "0" * 64, "data": b64.encode(b"abc")}
    w.be.vault.set("MY_KEY", "sk-live/abc+123=XYZ_9876")
    msg = {"thread": "7", "reply_to": None, "text": "Hello from Bob. key=sk-live/abc+123=XYZ_9876", "files": [good, bad]}
    assert w.arrive(BOB, "message", msg, env_id="m1").status_code == 202
    items = ok(w.c.get(f"/api/threads/{f['thread_id']}/items"))["items"]
    m = items[-1]["message"]
    assert m["sender"] == "peer" and m["via"] == "peer" and "Hello from Bob" in m["body"] and "sk-live" not in m["body"] and "[secret:MY_KEY]" in m["body"]
    assert "evil.sh" in m["body"] and "was left out" in m["body"]
    assert [a["path"] for a in m["attachments"]] == ["_inbox/peers/bob@other.example/notes.txt"]
    assert (w.inbox / "peers" / BOB / "notes.txt").read_bytes() == data
    assert not list((w.inbox / "peers" / BOB).glob("evil*"))
    assert ok(w.c.get(f"/api/threads/{f['thread_id']}"))["unread"] == 1
    assert w.note()[-1]["title"] == "Bob@other.example's assistant wrote" or "assistant wrote" in w.note()[-1]["title"]

    # The same envelope again: a duplicate, handed on once.
    r = w.arrive(BOB, "message", msg, env_id="m1")
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert len(ok(w.c.get(f"/api/threads/{f['thread_id']}/items"))["items"]) == 1

    # It reaches the agent, labelled as untrusted data (and not as the owner).
    text = ok(w.c.get("/api/agent/inbox", headers={"X-Agent-Token": "agent-secret"}))["text"]
    assert "Friend bob@other.example" in text and "untrusted" in text and "Hello from Bob" in text and "/work/_inbox/peers/bob@other.example/notes.txt" in text
    assert "Owner" not in text.split("untrusted")[0].split("\n")[-1]
    assert ok(w.c.get("/api/agent/inbox", headers={"X-Agent-Token": "agent-secret"}))["text"] == ""


def test_a_held_run_is_not_resumed_by_a_friends_message(w):
    tid = w.befriend()
    w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "ping", "files": []})
    assert w.be.conv.deliver(peek=True, peers=False)[0] == ""
    assert "ping" in w.be.conv.deliver(peek=True)[0]
    assert tid


# ── who may call the inbox ─────────────────────────────────────────────────
def test_only_the_tunnel_reaches_the_inbox(w):
    body = json.dumps({"from": BOB, "kind": "presence", "id": "p", "iat": "2026-11-22T10:00:00Z", "fingerprint": BOB_FP, "body": {"state": "idle"}}).encode()
    assert w.c.post("/api/peer/v0/inbox", content=body).status_code == 404  # the owner's session, from the LAN
    assert w.c.post("/api/peer/v0/inbox", content=body, headers={"X-Agent-Token": "agent-secret"}).status_code == 404
    assert w.c.get("/api/peer/v0/inbox").status_code == 404
    assert w.gw.paths().count("/v0/peer/open") == 0
    assert w.t.post("/api/peer/v0/inbox", content=body).status_code == 202
    # The sandbox's own network is refused outright, like everything but /api/agent.
    sandbox = TestClient(w.t.app, client=("10.255.255.9", 4000))
    assert sandbox.post("/api/peer/v0/inbox", content=body).status_code == 403
    # Not part of the owner API's schema.
    assert not [p for p in w.c.get("/api/openapi.json").json()["paths"] if p.startswith("/api/peer")]


def test_size_limit_is_refused_before_the_gateway(w):
    r = w.t.post("/api/peer/v0/inbox", content=b"x" * (16 * 1024 * 1024 + 1))
    assert r.status_code == 413 and r.json()["error"] == "too_large" and r.json()["v"] == "l0w/0.1"
    assert "/v0/peer/open" not in w.gw.paths()


def test_gateway_errors_become_the_specs_answers(w):
    def send(error):
        return w.t.post("/api/peer/v0/inbox", content=json.dumps({"_error": error}).encode())

    r = send((403, "no_token", ""))
    assert r.status_code == 403 and r.json() == {"v": "l0w/0.1", "error": "no_token", "detail": ""}
    r = send((401, "bad_signature", "secret reason"))
    assert r.status_code == 401 and r.json()["detail"] == ""  # never says which check failed
    assert send((404, "not_here", "")).status_code == 404
    assert send((400, "bad_seal", "")).status_code == 400
    assert send((422, "unknown_kind", "kind 'x'")).status_code == 422
    r = send((503, "unresolvable", "directory"))
    assert r.status_code == 503 and r.headers["retry-after"] == "30"  # the sender retries
    r = send((401, "unauthorized", ""))  # control's own token is wrong: not the sender's fault
    assert r.status_code == 503
    w.gw.down = True
    r = w.t.post("/api/peer/v0/inbox", content=b"{}")
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


def test_rate_limits_answer_429(w):
    w.be.friends_.per_sender.limit = 2
    for i in range(2):
        assert w.arrive(BOB, "presence", {"state": "idle"}, env_id=f"p{i}").status_code == 202
    r = w.arrive(BOB, "presence", {"state": "idle"}, env_id="p9")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"] == "rate_limited"
    assert w.arrive("ana@hub.example", "presence", {"state": "idle"}, env_id="a1").status_code == 202  # others are not held back
    w.be.friends_.overall.limit = 1
    assert w.arrive("cy@hub.example", "presence", {"state": "idle"}, env_id="c1").status_code == 429


def test_friend_requests_are_limited_per_sender_per_day(w):
    w.ask = lambda i: w.arrive(BOB, "friend-request", {"code": None, "note": str(i), "by": "owner", "token": TOKEN}, env_id=f"r{i}")
    w.be.friends_.settings.set("FRIEND_REQUESTS_FROM", "anyone")
    codes = [w.ask(i).status_code for i in range(11)]
    assert codes[:10] == [202] * 10 and codes[10] == 429


# ── who may send friend requests ───────────────────────────────────────────
def test_codes_only_drops_a_request_without_a_code(w):
    assert ok(w.c.patch("/api/friends/settings", json={"requests_from": "codes"}))["requests_from"] == "codes"
    r = w.arrive(BOB, "friend-request", {"code": None, "note": "hello", "by": "owner", "token": TOKEN})
    assert r.status_code == 202  # answered as usual, dropped silently
    assert ok(w.c.get("/api/friends")) == [] and ok(w.c.get("/api/friends/requests")) == []
    # The same envelope is a duplicate afterwards (it was recorded).
    assert w.arrive(BOB, "friend-request", {"code": None, "note": "hello", "by": "owner", "token": TOKEN}, env_id="env-1").status_code == 200


def test_a_friend_code_lets_a_request_through_once_and_only_its_hash_is_kept(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "codes"}))
    made = ok(w.c.post("/api/friends/codes", json={"label": "Bob", "idempotency_key": "kc1"}), 201)
    assert made["code"].startswith("l0wf1:mara@hub.example:WZCY8FG5JZ5EV87N:") and made["fingerprint"] == OUR_FP and made["address"] == ME
    secret = made["code"].rsplit(":", 1)[1]
    row = w.be.db.q1("SELECT * FROM friend_codes")
    assert row["secret_sha256"] == hashlib.sha256(secret.encode()).hexdigest() and secret not in json.dumps(row)
    assert ok(w.c.get("/api/friends/codes"))[0]["state"] == "active" and "code" not in ok(w.c.get("/api/friends/codes"))[0]
    # the same key again: it can't show the code twice
    assert w.c.post("/api/friends/codes", json={"label": "Bob", "idempotency_key": "kc1"}).status_code == 409
    assert len(w.be.db.q("SELECT 1 FROM friend_codes")) == 1

    w.arrive(BOB, "friend-request", {"code": "not-the-secret", "note": "x", "by": "owner", "token": TOKEN}, env_id="a")
    assert ok(w.c.get("/api/friends/requests")) == []
    assert w.arrive(BOB, "friend-request", {"code": secret, "note": "x", "by": "owner", "token": TOKEN}, env_id="b").status_code == 202
    req = ok(w.c.get("/api/friends/requests"))
    assert len(req) == 1 and req[0]["code_used"] is True and req[0]["state"] == "pending"
    assert ok(w.c.get("/api/friends/codes"))[0]["state"] == "used" and ok(w.c.get("/api/friends/codes"))[0]["used_by"] == BOB
    # spent: a second request with it is dropped
    w.arrive("eve@other.example", "friend-request", {"code": secret, "note": "x", "by": "owner", "token": TOKEN}, env_id="c")
    assert [r["address"] for r in ok(w.c.get("/api/friends/requests"))] == [BOB]


def test_expired_code_does_not_count(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "codes"}))
    made = ok(w.c.post("/api/friends/codes", json={"expires_days": 1}), 201)
    w.be.db.ex("UPDATE friend_codes SET expires_at='2020-01-01T00:00:00Z'")
    w.arrive(BOB, "friend-request", {"code": made["code"].rsplit(":", 1)[1], "note": "", "by": "owner", "token": TOKEN})
    assert ok(w.c.get("/api/friends/requests")) == []
    assert ok(w.c.get("/api/friends/codes"))[0]["state"] == "expired"


def test_hub_setting_lets_members_of_my_hub_in(w):
    assert ok(w.c.get("/api/friends/settings"))["requests_from"] == "hub"  # the default
    w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN})
    assert ok(w.c.get("/api/friends/requests")) == []
    w.arrive("ana@hub.example", "friend-request", {"code": None, "note": "hi", "by": "owner", "token": TOKEN})
    assert [r["address"] for r in ok(w.c.get("/api/friends/requests"))] == ["ana@hub.example"]
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "anyone"}))
    w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN})
    assert {r["address"] for r in ok(w.c.get("/api/friends/requests"))} == {"ana@hub.example", BOB}


# ── approving, declining, unfriending ──────────────────────────────────────
def test_approving_an_incoming_request_pins_then_sends_our_acceptance(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "anyone"}))
    w.arrive(BOB, "friend-request", {"code": None, "note": "<b>hi</b> it's Bob", "by": "assistant", "token": TOKEN}, env_id="req-1")
    [req] = ok(w.c.get("/api/friends/requests"))
    assert req["direction"] == "in" and req["made_by"] == "assistant" and req["fingerprint"] == BOB_FP and req["note"] == "<b>hi</b> it's Bob"
    assert ok(w.c.get("/api/friends"))[0]["state"] == "requested_in"
    assert w.note()[-1]["title"] == "Friend request"
    assert w.be.db.q1("SELECT COUNT(*) AS n FROM threads WHERE type='peer'")["n"] == 0  # a request is a card, never a thread

    done = ok(w.c.post(f"/api/friends/requests/{req['id']}/approve"))
    assert done["state"] == "approved"
    assert w.gw.bodies("/v0/peer/pin")[-1] == {"address": BOB, "fingerprint": BOB_FP, "held_token": TOKEN}
    send = w.gw.bodies("/v0/peer/send")[-1]
    assert send["kind"] == "friend-accept" and send["body"] == {"request": "req-1"} and send["request_token"] is True
    f = ok(w.c.get("/api/friends"))[0]
    assert f["state"] == "friends" and f["thread_id"]
    assert w.c.post(f"/api/friends/requests/{req['id']}/approve").status_code == 409  # already decided


def test_a_failed_accept_leaves_the_request_waiting(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "anyone"}))
    w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN})
    [req] = ok(w.c.get("/api/friends/requests"))
    w.gw.errors["/v0/peer/pin"] = (409, "key_changed", "")
    assert w.c.post(f"/api/friends/requests/{req['id']}/approve").status_code == 409
    assert ok(w.c.get("/api/friends/requests"))[0]["state"] == "pending" and ok(w.c.get("/api/friends"))[0]["state"] == "requested_in"
    w.gw.errors["/v0/peer/send"] = (409, "no_tunnel", "")
    assert w.c.post(f"/api/friends/requests/{req['id']}/approve").status_code == 409
    assert ok(w.c.get("/api/friends/requests"))[0]["state"] == "pending" and ok(w.c.get("/api/friends"))[0]["state"] == "requested_in"


def test_declining_says_nothing_and_forgets_them(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "anyone"}))
    w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN})
    [req] = ok(w.c.get("/api/friends/requests"))
    assert ok(w.c.post(f"/api/friends/requests/{req['id']}/decline"))["state"] == "declined"
    assert ok(w.c.get("/api/friends")) == [] and "/v0/peer/send" not in w.gw.paths()


def test_unfriending_tells_them_then_revokes_and_forgets(w):
    tid = w.befriend()
    w.gw.calls.clear()
    ok(w.c.delete(f"/api/friends/{BOB}"))
    assert w.gw.paths() == ["/v0/peer/send", "/v0/peer/revoke", "/v0/peer/forget"]  # told first, so the queued entry still delivers
    assert w.gw.bodies("/v0/peer/send")[0]["kind"] == "unfriend" and w.gw.bodies("/v0/peer/revoke") == [{"address": BOB}]
    assert ok(w.c.get("/api/friends")) == []
    items = ok(w.c.get(f"/api/threads/{tid}/items"))["items"]  # the thread stays as history
    assert "no longer friends" in items[-1]["message"]["body"]


def test_they_unfriend_us(w):
    tid = w.befriend()
    assert w.arrive(BOB, "unfriend", {}).status_code == 202
    assert ok(w.c.get("/api/friends")) == []
    assert w.gw.paths()[-2:] == ["/v0/peer/revoke", "/v0/peer/forget"]
    assert "ended the friendship" in ok(w.c.get(f"/api/threads/{tid}/items"))["items"][-1]["message"]["body"]
    assert w.note()[-1]["title"] == "Friendship ended"


def test_blocking_drops_everything_they_send(w):
    tid = w.befriend()
    blocked = ok(w.c.post(f"/api/friends/{BOB}/block"))
    assert blocked["state"] == "blocked"
    assert w.gw.paths()[-2:] == ["/v0/peer/revoke", "/v0/peer/forget"]
    assert w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "hello?", "files": []}).status_code == 202
    assert w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN}).status_code == 202
    assert ok(w.c.get(f"/api/threads/{tid}/items"))["items"] == [] and ok(w.c.get("/api/friends/requests")) == []
    assert w.c.post("/api/friends/requests", json={"address": BOB}).status_code == 409  # unblock first
    ok(w.c.post(f"/api/friends/{BOB}/unblock"))
    assert ok(w.c.get("/api/friends")) == []


def test_muted_friends_are_stored_but_not_announced(w):
    tid = w.befriend()
    ok(w.c.patch(f"/api/friends/{BOB}", json={"muted": True, "share": "shared", "waking": "ask", "presence_detail": "exact", "display_name": "Bobby"}))
    f = ok(w.c.get("/api/friends"))[0]
    assert (f["muted"], f["share"], f["waking"], f["presence_detail"], f["display_name"]) == (True, "shared", "ask", "exact", "Bobby")
    w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "quiet hello", "files": []})
    assert len(ok(w.c.get(f"/api/threads/{tid}/items"))["items"]) == 1 and w.note() == []


def test_presence_wake_and_same_owner_are_stored_and_announced_only(w):
    tid = w.befriend()
    w.arrive(BOB, "presence", {"state": "working", "next": "tonight", "now": "fixing the pump"}, env_id="p2", iat="2026-11-22T11:00:00Z")
    w.arrive(BOB, "presence", {"state": "asleep", "next": None, "now": None}, env_id="p1", iat="2026-11-22T10:00:00Z")  # a retry that was overtaken
    f = ok(w.c.get("/api/friends"))[0]
    assert f["presence"]["state"] == "working" and f["presence"]["now"] == "fixing the pump" and f["presence"]["at"].startswith("2026-11-22T11:00:00")
    w.arrive(BOB, "wake-request", {"reason": "need the test results"})
    w.arrive(BOB, "wake-reply", {"request": "x", "outcome": "later"})
    w.arrive(BOB, "same-owner", {"statement": {"v": "l0w/0.1", "type": "same-owner", "payload": "x", "sigs": []}})
    assert ok(w.c.get("/api/friends"))[0]["same_owner"] is True
    texts = [i["message"]["body"] for i in ok(w.c.get(f"/api/threads/{tid}/items"))["items"]]
    assert any("asked to wake" in t and "need the test results" in t for t in texts) and any("answered the wake request: later" in t for t in texts)
    assert not w.be.conv.deliver(peek=True)[0]  # none of it reaches the agent
    # Waking is "never" by default: refused without bothering the owner (the thread says so; test_friends_agent.py covers ask and allow).
    assert [n["title"] for n in w.note()] == [f"{BOB} answered your wake request", f"{BOB} says you're the same owner"]
    assert any("“never”" in t for t in texts)


# ── pausing ────────────────────────────────────────────────────────────────
def test_paused_refuses_incoming_and_holds_outgoing(w):
    tid = w.befriend()
    ok(w.c.patch("/api/friends/settings", json={"paused": True}))
    r = w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "hi", "files": []})
    assert r.status_code == 503 and r.json()["error"] == "paused" and r.headers["retry-after"]
    assert "/v0/peer/open" not in w.gw.paths()
    assert w.c.post("/api/friends/requests", json={"address": "ana@hub.example"}).status_code == 409

    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "are you there?", "idempotency_key": "m-1"}), 201)
    w.run(w.be.friends_.flush())
    assert "/v0/peer/send" not in w.gw.paths()  # waits
    ok(w.c.patch("/api/friends/settings", json={"paused": False}))
    w.run(w.be.friends_.flush())
    assert w.gw.bodies("/v0/peer/send")[0]["body"]["text"] == "are you there?"
    assert w.arrive(BOB, "message", {"thread": "1", "reply_to": None, "text": "yes", "files": []}).status_code == 202


# ── idempotency ────────────────────────────────────────────────────────────
def test_creating_a_request_twice_with_one_key_sends_once(w):
    a = ok(w.c.post("/api/friends/requests", json={"address": BOB, "idempotency_key": "same"}), 201)
    b = ok(w.c.post("/api/friends/requests", json={"address": BOB, "idempotency_key": "same"}), 201)
    assert a == b and len(w.gw.bodies("/v0/peer/send")) == 1
    assert w.c.post("/api/friends/requests", json={"address": BOB, "idempotency_key": "other"}).status_code == 409  # already asked


def test_a_message_posted_twice_with_one_key_is_sent_once(w):
    tid = w.befriend()
    for _ in range(2):
        ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "once", "idempotency_key": "dup"}), 201)
    w.run(w.be.friends_.flush())
    assert len(w.gw.bodies("/v0/peer/send")) == 1


# ── asking with a code, and what can go wrong ──────────────────────────────
def test_requesting_with_a_code_checks_its_fingerprint(w):
    code = f"l0wf1:{BOB}:{BOB_FP.replace('-', '')}:GJrlKV4Takm_GadfHT3HVg"
    r = ok(w.c.post("/api/friends/requests", json={"code": code, "note": "from the code"}), 201)
    assert r["address"] == BOB and r["code_used"] is True
    assert w.gw.bodies("/v0/peer/pin")[0] == {"address": BOB, "fingerprint": BOB_FP}
    assert w.gw.bodies("/v0/peer/send")[0]["body"]["code"] == "GJrlKV4Takm_GadfHT3HVg"
    assert w.be.db.q1("SELECT code FROM friend_requests")["code"] is None  # not kept once sent

    # A mismatch is the gateway's call: nothing is stored.
    w.be.db.ex("DELETE FROM friends")
    w.gw.errors["/v0/peer/pin"] = (409, "fingerprint_mismatch", "")
    r = w.c.post("/api/friends/requests", json={"code": code})
    assert r.status_code == 409 and r.json()["error"] == "fingerprint_mismatch"
    assert len(ok(w.c.get("/api/friends/requests"))) == 1 and ok(w.c.get("/api/friends")) == []
    assert w.c.post("/api/friends/requests", json={"code": "nonsense"}).status_code == 400
    assert w.c.post("/api/friends/requests", json={"address": "ana@hub.example", "code": code}).status_code == 400
    assert w.c.post("/api/friends/requests", json={"address": "nobody"}).status_code == 400
    assert w.c.post("/api/friends/requests", json={"address": ME}).status_code == 400


def test_a_changed_key_is_an_alarm_never_accepted(w):
    w.befriend()
    w.gw.errors["/v0/peer/pin"] = (409, "key_changed", "")
    w.run(w.be.friends_._check_keys(utcnow()))
    f = ok(w.c.get("/api/friends"))[0]
    assert f["key_alarm"] and f["state"] == "friends"
    assert [n["title"] for n in w.note()] == [f"{w.be.friends_._friend_row(BOB)['display_name']}'s key changed"]
    # It stays until a check comes back clean.
    w.be.db.ex("UPDATE friends SET checked_at=NULL")
    w.run(w.be.friends_._check_keys(utcnow()))
    assert ok(w.c.get("/api/friends"))[0]["key_alarm"] is None


def test_the_owners_fingerprint(w):
    assert ok(w.c.get("/api/friends/fingerprint")) == {"address": ME, "fingerprint": OUR_FP}


def test_friends_need_a_hub(w):
    w.be.db.ex("UPDATE hub_membership SET state='left'")
    assert w.c.post("/api/friends/requests", json={"address": BOB}).status_code == 409
    assert w.t.post("/api/peer/v0/inbox", content=b"{}").status_code == 404


# ── our messages out ───────────────────────────────────────────────────────
def test_the_owners_message_is_sent_and_its_delivery_follows_the_outbox(w):
    tid = w.befriend()
    w.be.vault.set("MY_KEY", "sk-live/abc+123=XYZ_9876")
    m = ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "the key is sk-live/abc+123=XYZ_9876", "idempotency_key": "m-1"}), 201)
    w.run(w.be.friends_.flush())
    send = w.gw.bodies("/v0/peer/send")[0]
    assert send["to"] == BOB and send["kind"] == "message"
    assert send["body"] == {"thread": str(tid), "reply_to": None, "text": "the key is [secret:MY_KEY]", "files": []}
    ob = w.be.db.q1("SELECT * FROM peer_outbox")
    assert ob["state"] == "queued" and ob["envelope_id"] == "out-1" and ob["message_id"] == m["id"]
    assert ok(w.c.get(f"/api/threads/{tid}/items"))["items"][-1]["message"]["delivery"] == "queued"

    # The gateway delivers it later; housekeeping reads that back (the feed repeats entries: idempotent).
    w.gw.outbox = [{"id": "out-1", "to": BOB, "kind": "message", "state": "delivered", "error": None, "updated": "2026-11-22T10:05:00Z"}]
    for _ in range(2):
        w.run(w.be.friends_.housekeeping(utcnow()))
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "delivered"
    assert ok(w.c.get(f"/api/threads/{tid}/items"))["items"][-1]["message"]["delivery"] == "delivered"
    assert w.be.db.q1("SELECT value FROM kv WHERE key='peer_outbox_since'")["value"] == "2026-11-22T10:05:00Z"


AGENT = {"X-Agent-Token": "agent-secret"}


def test_the_assistants_files_go_out_with_its_message(w, tmp_path):
    tid = w.befriend()
    (tmp_path / "work" / "reports").mkdir()
    (tmp_path / "work" / "reports" / "pump.csv").write_bytes(b"a,b\n1,2\n")
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "notes.txt").write_bytes(b"hello")
    r = ok(w.c.post("/api/agent/messages", headers=AGENT, json={"thread": tid, "body": "results attached", "files": ["/work/reports/pump.csv", "/workspace/notes.txt"]}))
    assert [(a["name"], a["volume"], a["size"]) for a in r["attachments"]] == [("pump.csv", "work", 8), ("notes.txt", "workspace", 5)]
    w.run(w.be.friends_.flush())
    files = w.gw.bodies("/v0/peer/send")[0]["body"]["files"]
    assert [(f["name"], f["type"], f["size"], f["sha256"], b64.decode(f["data"])) for f in files] == [
        ("pump.csv", "text/csv", 8, hashlib.sha256(b"a,b\n1,2\n").hexdigest(), b"a,b\n1,2\n"),
        ("notes.txt", "text/plain", 5, hashlib.sha256(b"hello").hexdigest(), b"hello")]


def test_the_assistants_files_are_checked_before_anything_is_stored(w, tmp_path):
    tid = w.befriend()
    work = tmp_path / "work"
    (work / "secret-link").symlink_to("/etc/hostname")
    (work / "dir").mkdir()
    (work / "dir" / "real").write_bytes(b"x")
    (work / "linked-dir").symlink_to(work / "dir")
    (work / "big.bin").write_bytes(b"\0" * (5 * 1024 * 1024 + 1))
    (work / "ok.bin").write_bytes(b"\0" * (5 * 1024 * 1024))

    def post(*files):
        return w.c.post("/api/agent/messages", headers=AGENT, json={"thread": tid, "body": "x", "files": list(files)})

    for bad, err in [("/work/secret-link", "symlink"), ("/work/linked-dir/real", "symlink"), ("/work/missing", "file_not_found"), ("/work/dir", "not_a_file"),
                     ("/work/", "bad_file"), ("work/ok.bin", "bad_file"), ("/etc/passwd", "bad_file"), ("/work/../etc/passwd", "bad_path")]:
        r = post(bad)
        assert r.status_code == 400 and r.json()["error"] == err, (bad, r.text)
    r = post("/work/big.bin")
    assert r.status_code == 400 and r.json()["error"] == "files_too_large", r.text
    assert post("/work/ok.bin", "/work/dir/real").json()["error"] == "files_too_large"  # the total counts
    assert post(*["/work/dir/real"] * 11).json()["error"] == "too_many_files"
    assert ok(w.c.get(f"/api/threads/{tid}/items"))["items"] == []  # nothing was stored, so nothing is waiting to be sent
    assert post("/work/ok.bin").status_code == 200  # exactly 5 MiB is fine


def test_a_file_that_went_missing_before_sending_fails_the_message_and_tells_the_owner(w, tmp_path):
    tid = w.befriend()
    w.gw.down = True  # the message waits for the gateway, so there's time to swap the file
    (tmp_path / "work" / "a.txt").write_bytes(b"a")
    ok(w.c.post("/api/agent/messages", headers=AGENT, json={"thread": tid, "body": "see a.txt", "files": ["/work/a.txt"]}))
    (tmp_path / "work" / "a.txt").unlink()
    (tmp_path / "work" / "a.txt").symlink_to("/etc/hostname")  # swapped for a link after it was checked
    w.gw.down = False
    w.run(w.be.friends_.flush())
    assert w.gw.bodies("/v0/peer/send") == []
    last = ok(w.c.get(f"/api/threads/{tid}/items"))["items"][-1]["message"]
    assert last["sender"] == "system" and "Couldn't deliver" in last["body"] and "a.txt" in last["body"]
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "failed"


def test_the_owners_uploads_go_out_and_over_5_mib_is_refused(w):
    tid = w.befriend()

    def up(name, data):
        return ok(w.c.post("/api/uploads", files={"file": (name, data, "text/plain")}, data={"idempotency_key": f"u-{name}"}), 201)

    small = up("note.txt", b"for bob")
    m = ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "here", "upload_ids": [small["id"]], "idempotency_key": "m-f1"}), 201)
    assert [a["name"] for a in m["attachments"]] == ["note.txt"]
    w.run(w.be.friends_.flush())
    f = w.gw.bodies("/v0/peer/send")[0]["body"]["files"][0]
    assert (f["name"], f["type"], b64.decode(f["data"])) == ("note.txt", "text/plain", b"for bob")

    a, b = up("a.bin", b"\0" * (3 * 1024 * 1024)), up("b.bin", b"\0" * (3 * 1024 * 1024))
    r = w.c.post(f"/api/threads/{tid}/messages", json={"body": "too much", "upload_ids": [a["id"], b["id"]], "idempotency_key": "m-f2"})
    assert r.status_code == 400 and r.json()["error"] == "files_too_large", r.text
    assert len(ok(w.c.get(f"/api/threads/{tid}/items"))["items"]) == 1
    # In an ordinary thread the limit doesn't apply (nothing is sent anywhere).
    n = ok(w.c.post("/api/threads", json={"title": "Plain"}), 201)
    ok(w.c.post(f"/api/threads/{n['id']}/messages", json={"body": "big", "upload_ids": [a["id"], b["id"]], "idempotency_key": "m-f3"}), 201)


def test_a_final_failure_is_told_to_the_owner(w):
    tid = w.befriend()
    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "hello", "idempotency_key": "m-2"}), 201)
    w.run(w.be.friends_.flush())
    w.gw.outbox = [{"id": "out-1", "to": BOB, "kind": "message", "state": "failed", "error": "no_token", "updated": "2026-11-22T10:05:00Z"}]
    w.run(w.be.friends_.housekeeping(utcnow()))
    items = ok(w.c.get(f"/api/threads/{tid}/items"))["items"]
    assert items[-1]["message"]["sender"] == "system" and "Couldn't deliver" in items[-1]["message"]["body"] and "no_token" in items[-1]["message"]["body"]
    assert [n["title"] for n in w.note()] == [f"Message to {BOB} wasn't delivered"]
    w.run(w.be.friends_.housekeeping(utcnow()))  # nothing more happens the second time
    assert len(w.note()) == 1


def test_the_gateway_being_away_keeps_a_message_waiting(w):
    tid = w.befriend()
    w.gw.down = True
    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "later", "idempotency_key": "m-3"}), 201)
    w.run(w.be.friends_.flush())
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "pending"
    w.gw.down = False
    w.run(w.be.friends_.flush())
    assert w.be.db.q1("SELECT state FROM peer_outbox")["state"] == "queued" and len(w.gw.bodies("/v0/peer/send")) == 1


def test_a_request_the_other_side_refuses_is_marked_failed(w):
    w.gw.send_state = "failed"
    r = ok(w.c.post("/api/friends/requests", json={"address": BOB}), 201)
    assert r["state"] == "failed" and r["error"] == "their instance said no"
    assert ok(w.c.get("/api/friends")) == []
    assert w.note()[-1]["title"].startswith("Your friend request to")


def test_the_assistants_draft_waits_for_the_owner(w):
    draft = w.run(w.be.friends_.draft_request(BOB, "my assistant says hi"))
    assert draft.state == "pending" and draft.made_by == "assistant" and "/v0/peer/send" not in w.gw.paths()
    assert ok(w.c.get("/api/friends")) == []
    sent = ok(w.c.post(f"/api/friends/requests/{draft.id}/approve"))
    assert sent["state"] == "sent"
    send = w.gw.bodies("/v0/peer/send")[0]
    assert send["body"]["by"] == "assistant" and send["body"]["note"] == "my assistant says hi"
    assert ok(w.c.get("/api/friends"))[0]["state"] == "requested_out"


# ── the rest of the surface ────────────────────────────────────────────────
def test_security_and_scopes(w):
    # a `cli` device token can read friends but not change them
    token = ok(w.c.post("/api/account/devices", json={"label": "cli", "scope": "cli"}), 201)["device_token"]
    h = {"Authorization": f"Bearer {token}"}
    other = TestClient(w.c.app, client=("192.168.1.21", 5000))
    assert other.get("/api/friends", headers=h).status_code == 200
    assert other.post("/api/friends/requests", json={"address": BOB}, headers=h).status_code == 403
    assert other.patch("/api/friends/settings", json={"paused": True}, headers=h).status_code == 403


def test_sync_feed_and_live_events_carry_friends(w):
    w.befriend()
    snap = ok(w.c.get("/api/sync"))
    assert {"friend"} <= {c["entity"] for c in snap["changes"]}
    cursor = snap["cursor"]
    ok(w.c.patch(f"/api/friends/{BOB}", json={"muted": True}))
    changes = ok(w.c.get("/api/sync", params={"since": cursor}))["changes"]
    assert [(c["entity"], c["id"], c["data"]["muted"]) for c in changes if c["entity"] == "friend"] == [("friend", BOB, True)]


def test_expired_requests_and_old_envelope_ids_are_cleaned(w):
    ok(w.c.patch("/api/friends/settings", json={"requests_from": "anyone"}))
    w.arrive(BOB, "friend-request", {"code": None, "note": "", "by": "owner", "token": TOKEN})
    w.be.db.ex("UPDATE friend_requests SET created_at='2020-01-01T00:00:00Z'")
    w.be.db.ex("UPDATE peer_envelopes SET received_at='2020-01-01T00:00:00Z'")
    w.run(w.be.friends_.housekeeping(utcnow()))
    assert ok(w.c.get("/api/friends/requests"))[0]["state"] == "expired" and ok(w.c.get("/api/friends")) == []
    assert w.be.db.q("SELECT 1 FROM peer_envelopes") == []


def _wait(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_posting_in_a_peer_thread_sends_without_holding_the_request(w):
    tid = w.befriend()
    ok(w.c.post(f"/api/threads/{tid}/messages", json={"body": "ping", "idempotency_key": "m-9"}), 201)
    assert _wait(lambda: w.gw.bodies("/v0/peer/send"))  # the background flush
    # A normal thread is never sent anywhere.
    n = ok(w.c.post("/api/threads", json={"title": "Plain"}), 201)
    ok(w.c.post(f"/api/threads/{n['id']}/messages", json={"body": "private", "idempotency_key": "m-10"}), 201)
    time.sleep(0.2)
    assert len(w.gw.bodies("/v0/peer/send")) == 1
