"""Friends and messages: two gateways, a fake directory (httpx.MockTransport) and the outbox."""

import asyncio
import json
import time

import httpx
import pytest
from l0wproto import b64, docs, keys, messages, payloads
from l0wproto.consts import MAX_ENVELOPE
from l0wproto.keys import Key
from l0wproto.times import format_time, parse_time
from starlette.testclient import TestClient

from l0w_gateway import create_app, safefetch
from l0w_gateway.peers import Net

CT, TT = "control-secret", "tunnel-secret"
H = {"Authorization": f"Bearer {CT}"}
T0 = int(time.time())


class World:
    """Hubs a.example and b.example (a directory each) and the members' instance hostnames; records what is delivered."""

    def __init__(self):
        self.hubs = {d: keys.generate("sign") for d in ("a.example", "b.example")}
        self.entries: dict[str, dict] = {}  # address -> directory entry
        self.inbox_host: dict[str, str] = {}  # instance hostname -> address
        self.delivered: list[tuple[str, dict]] = []  # (host, envelope)
        self.mode: object = "ok"  # "ok" | "down" | an int status | (status, body)
        self.directory_calls = 0

    def hub_doc(self, domain: str) -> dict:
        kp = self.hubs[domain]
        p = payloads.build_hub(domain.split(".")[0], domain, [kp.key], f"https://{domain}/api/", f"wss://{domain}/tunnel")
        return docs.sign_document("hub", p, [kp])

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/.well-known/l0wagent/hub":
            self.directory_calls += 1
            return httpx.Response(200, json=self.hub_doc(host))
        if path.startswith("/.well-known/l0wagent/members/"):
            e = self.entries.get(f"{path.rsplit('/', 1)[1]}@{host}")
            return httpx.Response(200, json=e) if e else httpx.Response(404, json={"error": "no_such_member"})
        if path == "/api/peer/v0/inbox":
            if self.mode == "down":
                raise httpx.ConnectError("down")
            self.delivered.append((host, json.loads(request.content)))
            if isinstance(self.mode, tuple):
                return httpx.Response(self.mode[0], json=self.mode[1])
            return httpx.Response(202 if self.mode == "ok" else self.mode, json={"v": "l0w/0.1"})
        return httpx.Response(404)


class Member:
    def __init__(self, world: World, tmp, name: str, domain: str, **app_kw):
        self.world, self.name, self.domain = world, name, domain
        self.address = f"{name}@{domain}"
        self.clock = [T0 + 0.0]
        self.hosts = {"instance": f"{name}.{domain}", "files": f"{name}-files.{domain}"}
        self.dir = tmp / name
        self.app = create_app(self.dir, CT, TT, net=Net(transport=httpx.MockTransport(world.handler), allow_http=False, connect_to=None),
                              clock=lambda: self.clock[0], **app_kw)
        self.c = TestClient(self.app)
        r = self.post("/v0/register", {"hub": domain, "name": name, "invite": "i", "release": "0.9.0", "protocols": ["l0w/0.1"]})
        assert r.status_code == 200, r.text
        assert self.c.put("/v0/tunnel", json={"config": {"url": "wss://x", "address": self.address, "hub": domain, "hostnames": self.hosts, "release": "0.9.0"}},
                          headers=H).status_code == 200
        world.inbox_host[self.hosts["instance"]] = self.address
        self.seq = 0
        self.publish()

    def post(self, path, body=None, **kw):
        return self.c.post(path, json=body, headers=H, **kw)

    def publish(self):
        self.seq += 1
        r = self.post("/v0/sign/self", {"address": self.address, "seq": self.seq, "hostnames": self.hosts, "protocols": ["l0w/0.1"]})
        assert r.status_code == 200, r.text
        self.world.entries[self.address] = docs.countersign_directory(r.json()["doc"], self.world.hubs[self.domain])

    @property
    def fp(self) -> str:
        return self.c.get("/v0/peer/fingerprint", headers=H).json()["fingerprint"]

    def peers(self) -> dict:
        f = self.dir / "peers.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def outbox(self) -> list:
        f = self.dir / "outbox.json"
        return json.loads(f.read_text()) if f.exists() else []

    def pin(self, other: "Member", **kw):
        return self.post("/v0/peer/pin", {"address": other.address, **kw})

    def send(self, to: "Member", kind: str, body: dict, **kw):
        return self.post("/v0/peer/send", {"to": to.address, "kind": kind, "body": body, **kw})

    def open(self, doc: dict):
        return self.c.post("/v0/peer/open", content=json.dumps(doc), headers={**H, "Content-Type": "application/json"})

    def last_delivered(self) -> dict:
        return self.world.delivered[-1][1]

    def keypair(self, use: str) -> keys.KeyPair:
        e = json.loads((self.dir / "keys.json").read_text())[use]
        return keys.from_seed(b64.decode(e["seed"]), use, e["pub"]["created"])


@pytest.fixture(autouse=True)
def no_dev_env(monkeypatch):
    monkeypatch.delenv("L0W_HUB_ALLOW_HTTP", raising=False)
    monkeypatch.delenv("L0W_HUB_CONNECT_TO", raising=False)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def mara(world, tmp_path):
    return Member(world, tmp_path, "mara", "a.example")


@pytest.fixture
def bob(world, tmp_path):
    return Member(world, tmp_path, "bob", "b.example")


def befriend(mara: Member, bob: Member) -> None:
    """mara asks, bob accepts: both sides then hold the other's token."""
    assert mara.pin(bob, fingerprint=bob.fp).status_code == 200
    r = mara.send(bob, "friend-request", {"code": None, "note": "hi", "by": "owner"}, request_token=True)
    assert r.status_code == 200 and r.json()["state"] == "delivered", r.text
    got = bob.open(mara.last_delivered())
    assert got.status_code == 200, got.text
    req = got.json()
    assert bob.pin(mara, fingerprint=req["fingerprint"], held_token=req["body"]["token"]).status_code == 200
    r = bob.send(mara, "friend-accept", {"request": req["id"]}, request_token=True)
    assert r.json()["state"] == "delivered", r.text
    assert mara.open(bob.last_delivered()).status_code == 200


def test_peer_endpoints_need_the_control_token(mara):
    for method, path in [("GET", "/v0/peer/fingerprint"), ("POST", "/v0/peer/pin"), ("POST", "/v0/peer/token"), ("POST", "/v0/peer/revoke"),
                         ("POST", "/v0/peer/forget"), ("POST", "/v0/peer/send"), ("GET", "/v0/peer/outbox"), ("POST", "/v0/peer/open"), ("PUT", "/v0/peer/services")]:
        for h in ({}, {"Authorization": f"Bearer {TT}"}, {"Authorization": "Bearer nope"}):
            assert mara.c.request(method, path, json={}, headers=h).status_code == 401, (method, path)


def test_pin_returns_fingerprint_and_hostnames(mara, bob):
    r = mara.pin(bob)
    assert r.status_code == 200, r.text
    assert r.json() == {"address": bob.address, "fingerprint": bob.fp, "hostnames": bob.hosts, "rotated": False,
                        "sign_key": bob.keypair("sign").key.to_dict(), "seal_key": bob.keypair("seal").key.to_dict()}
    assert mara.peers()[bob.address]["sign_key"]["kid"] == bob.keypair("sign").kid
    assert (mara.dir / "peers.json").stat().st_mode & 0o777 == 0o600


def test_pin_checks_the_fingerprint(mara, bob):
    r = mara.pin(bob, fingerprint="AAAA-AAAA-AAAA-AAAA")
    assert r.status_code == 409 and r.json()["error"] == "fingerprint_mismatch"
    assert bob.address not in mara.peers()
    assert mara.pin(bob, fingerprint=bob.fp.replace("-", "").lower()).status_code == 200


def test_pin_unknown_member_and_bad_address(mara):
    assert mara.post("/v0/peer/pin", {"address": "ghost@b.example"}).status_code == 404
    assert mara.post("/v0/peer/pin", {"address": "not-an-address"}).status_code == 400


def test_friend_request_and_accept_swap_tokens(mara, bob):
    befriend(mara, bob)
    pm, pb = mara.peers()[bob.address], bob.peers()[mara.address]
    assert pm["issued_token"]["revoked"] is False and pm["held_token"] is not None
    assert pb["issued_token"]["revoked"] is False and pb["held_token"] is not None
    assert messages.token_id(pm["held_token"]) == pb["issued_token"]["id"]
    assert messages.token_id(pb["held_token"]) == pm["issued_token"]["id"]


def test_friend_request_does_not_pin_the_sender(mara, bob):
    assert mara.pin(bob).status_code == 200
    assert mara.send(bob, "friend-request", {"code": None, "note": "", "by": "owner"}, request_token=True).status_code == 200
    got = bob.open(mara.last_delivered()).json()
    assert got["kind"] == "friend-request" and got["from"] == mara.address and got["fingerprint"] == mara.fp
    assert got["body"]["token"]["type"] == "delivery-token"
    assert mara.address not in bob.peers()


def test_message_round_trip_and_seq(mara, bob):
    befriend(mara, bob)
    for i in (1, 2):
        r = mara.send(bob, "message", {"thread": "t1", "reply_to": None, "text": f"hello {i}", "files": []})
        assert r.json()["state"] == "delivered"
        got = bob.open(mara.last_delivered())
        assert got.status_code == 200, got.text
        m = got.json()
        assert (m["from"], m["kind"], m["body"]["text"], m["fingerprint"]) == (mara.address, "message", f"hello {i}", mara.fp)
        assert m["id"] == r.json()["id"]
    assert m["seq"] == mara.peers()[bob.address]["seq_out"] == 3  # the friend-request was 1
    assert "hello" not in json.dumps(mara.last_delivered())  # sealed on the wire
    back = bob.send(mara, "message", {"thread": "t", "reply_to": None, "text": "hi back", "files": []})
    assert back.json()["state"] == "delivered"
    assert mara.open(bob.last_delivered()).json()["body"]["text"] == "hi back"


def test_send_needs_a_pin_and_a_token(mara, bob):
    assert mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}).json()["error"] == "not_pinned"
    mara.pin(bob)
    r = mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []})
    assert r.status_code == 409 and r.json()["error"] == "no_token"
    assert mara.send(bob, "nonsense", {}).status_code == 422
    assert mara.outbox() == []
    befriend(mara, bob)
    assert mara.send(bob, "message", {"nope": 1}).status_code == 400
    assert len(mara.outbox()) == 1  # only the friend request: nothing was queued for the refused sends


def test_no_token_is_403(mara, bob):
    befriend(mara, bob)
    kp = mara.keypair("sign")
    seal = Key.from_dict(mara.peers()[bob.address]["seal_key"])
    p = payloads.build_envelope(mara.address, kp.kid, bob.address, seal.kid, payloads.new_id(), 9, "message", {"alg": "x", "enc": "AA", "ct": "AA"})
    r = bob.open(docs.sign_document("envelope", p, [kp]))
    assert r.status_code == 403 and r.json()["error"] == "no_token"


def test_token_of_a_stranger_or_unknown_is_403(mara, bob, world, tmp_path):
    befriend(mara, bob)
    eve = Member(world, tmp_path, "eve", "b.example")
    eve.pin(bob)
    kp = eve.keypair("sign")
    seal = Key.from_dict(eve.peers()[bob.address]["seal_key"])
    stolen = mara.peers()[bob.address]["held_token"]  # bob's token for mara, presented by eve
    doc = messages.make_envelope(kp, kp.kid, eve.address, bob.address, seal, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}, 1, stolen)
    assert bob.open(doc).status_code == 403


def test_revoked_token_is_403_and_a_new_one_works(mara, bob):
    befriend(mara, bob)
    body = {"thread": "t", "reply_to": None, "text": "x", "files": []}
    mara.send(bob, "message", body)
    assert bob.open(mara.last_delivered()).status_code == 200
    assert bob.post("/v0/peer/revoke", {"address": mara.address}).json()["revoked"] is True
    r = bob.open(mara.last_delivered())
    assert r.status_code == 403 and r.json()["error"] == "no_token"
    t = bob.post("/v0/peer/token", {"address": mara.address}).json()["token"]
    assert messages.token_id(t) != messages.token_id(mara.peers()[bob.address]["held_token"])  # a fresh one, not the old
    assert bob.post("/v0/peer/token", {"address": mara.address}).json()["token"] == t  # asking again returns it


def test_forget_removes_the_friend(mara, bob):
    befriend(mara, bob)
    assert bob.post("/v0/peer/forget", {"address": mara.address}).json()["forgotten"] is True
    assert mara.address not in bob.peers()
    mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []})
    assert bob.open(mara.last_delivered()).status_code == 403


def test_wrong_recipient_is_404(mara, bob):
    befriend(mara, bob)
    kp = mara.keypair("sign")
    seal = Key.from_dict(mara.peers()[bob.address]["seal_key"])
    doc = messages.make_envelope(kp, kp.kid, mara.address, "carol@b.example", seal, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}, 9,
                                 mara.peers()[bob.address]["held_token"])
    r = bob.open(doc)
    assert r.status_code == 404 and r.json()["error"] == "not_here"


def test_tampered_envelope_is_401_with_no_detail(mara, bob):
    befriend(mara, bob)
    mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []})
    doc = mara.last_delivered()
    p = json.loads(b64.decode(doc["payload"]))
    p["seq"] += 5
    doc["payload"] = b64.encode(json.dumps(p, separators=(",", ":")).encode())
    r = bob.open(doc)
    assert r.status_code == 401 and r.json() == {"error": "bad_signature", "detail": ""}


def test_envelope_signed_by_another_key_is_401(mara, bob, world, tmp_path):
    befriend(mara, bob)
    kp = mara.keypair("sign")
    seal = Key.from_dict(mara.peers()[bob.address]["seal_key"])
    other = keys.generate("sign")
    p = payloads.build_envelope(mara.address, kp.kid, bob.address, seal.kid, payloads.new_id(), 3, "message",
                                {"alg": "x", "enc": "AA", "ct": "AA"}, mara.peers()[bob.address]["held_token"])
    r = bob.open(docs.sign_document("envelope", p, [other]))
    assert r.status_code == 401


def test_unknown_kind_bad_seal_and_bad_body(mara, bob):
    befriend(mara, bob)
    kp = mara.keypair("sign")
    seal = Key.from_dict(mara.peers()[bob.address]["seal_key"])
    tok = mara.peers()[bob.address]["held_token"]
    # a valid envelope whose sealed body is for a kind the receiver doesn't know
    good = messages.make_envelope(kp, kp.kid, mara.address, bob.address, seal, "presence", {"state": "idle", "next": None, "now": None}, 4, tok)
    p = json.loads(b64.decode(good["payload"]))
    p["kind"] = "teleport"
    r = bob.open(docs.sign_document("envelope", p, [kp]))
    assert r.status_code == 422 and r.json()["error"] == "unknown_kind", r.text
    p["kind"] = "presence"
    p["sealed"]["ct"] = p["sealed"]["ct"][:-4] + "AAAA"
    r = bob.open(docs.sign_document("envelope", p, [kp]))
    assert r.status_code == 400 and r.json() == {"error": "bad_seal", "detail": ""}


def test_old_envelope_is_rejected(mara, bob):
    befriend(mara, bob)
    mara.clock[0] -= 9 * 24 * 3600
    mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []})
    assert bob.open(mara.last_delivered()).status_code == 400


def test_oversized_and_garbage_bodies(mara, bob):
    r = bob.c.post("/v0/peer/open", content=b"x" * (MAX_ENVELOPE + 1), headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    r = bob.c.post("/v0/peer/open", content=b"not json", headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400


def test_friend_request_from_a_key_that_is_not_the_directorys_is_401(mara, bob):
    mara.pin(bob)
    rogue = keys.generate("sign")
    seal = Key.from_dict(mara.peers()[bob.address]["seal_key"])
    doc = messages.make_envelope(rogue, rogue.kid, mara.address, bob.address, seal, "friend-request", {"code": None, "note": "", "by": "owner",
                                 "token": docs.sign_document("delivery-token", payloads.build_delivery_token(mara.address, rogue.kid, bob.address, rogue.kid), [rogue])},
                                 1, None)
    assert bob.open(doc).status_code == 401


# --- the outbox ---


def test_presence_replaces_older_queued_presence(mara, bob):
    befriend(mara, bob)
    mara.world.mode = "down"
    pres = lambda s: {"state": s, "next": None, "now": None}  # noqa: E731
    first = mara.send(bob, "presence", pres("idle")).json()
    mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []})
    second = mara.send(bob, "presence", pres("asleep")).json()
    assert first["state"] == second["state"] == "queued"
    ids = [(e["kind"], e["id"]) for e in mara.outbox()]
    assert ("presence", first["id"]) not in ids and ("presence", second["id"]) in ids
    assert sum(k == "presence" for k, _ in ids) == 1 and sum(k == "message" for k, _ in ids) == 1


def test_outbox_retries_back_off_and_deliver(mara, bob):
    befriend(mara, bob)
    mara.world.mode = "down"
    r = mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}).json()
    assert r["state"] == "queued" and r["error"].startswith("unreachable")
    pump = lambda: asyncio.run(mara.app.state.peers.pump())  # noqa: E731
    e = lambda: next(x for x in mara.outbox() if x["id"] == r["id"])  # noqa: E731
    assert (e()["attempts"], e()["next_at"]) == (1, format_time(T0 + 30))
    assert pump() == 0  # not due yet
    delays = []
    for _ in range(9):
        mara.clock[0] = max(parse(e()["next_at"]), mara.clock[0])
        assert pump() == 1
        delays.append(parse(e()["next_at"]) - mara.clock[0])
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]
    mara.world.mode = "ok"
    mara.clock[0] = parse(e()["next_at"])
    pump()
    assert e()["state"] == "delivered" and e()["error"] is None and e()["final_at"] == format_time(mara.clock[0])
    assert bob.open(mara.last_delivered()).status_code == 200  # the same bytes as the first try, still within the window
    assert pump() == 0
    view = mara.c.get("/v0/peer/outbox", headers=H).json()["entries"]
    assert all("doc" not in x and "host" not in x for x in view)


def parse(s: str) -> int:
    from l0wproto.times import parse_time

    return parse_time(s)


def test_duplicate_and_client_errors_are_final(mara, bob):
    befriend(mara, bob)
    body = {"thread": "t", "reply_to": None, "text": "x", "files": []}
    mara.world.mode = (200, {"duplicate": True})
    assert mara.send(bob, "message", body).json()["state"] == "delivered"
    mara.world.mode = (403, {"v": "l0w/0.1", "error": "no_token", "detail": ""})
    r = mara.send(bob, "message", body).json()
    assert (r["state"], r["error"]) == ("failed", "no_token")
    mara.world.mode = (429, {"error": "rate_limited"})
    assert mara.send(bob, "message", body).json()["state"] == "queued"
    mara.world.mode = 503
    assert mara.send(bob, "message", body).json()["state"] == "queued"
    mara.world.mode = (404, {})
    r = mara.send(bob, "message", body).json()
    assert (r["state"], r["error"]) == ("failed", "http_404")


def test_unfinished_entries_expire_after_7_days_and_finished_ones_are_dropped_after_14(mara, bob):
    befriend(mara, bob)
    mara.world.mode = "down"
    r = mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}).json()
    pump = lambda: asyncio.run(mara.app.state.peers.pump())  # noqa: E731
    mara.clock[0] = T0 + 7 * 24 * 3600 - 1
    pump()
    assert next(x for x in mara.outbox() if x["id"] == r["id"])["state"] == "queued"
    mara.clock[0] = T0 + 7 * 24 * 3600 + 3600
    pump()
    e = next(x for x in mara.outbox() if x["id"] == r["id"])
    assert (e["state"], e["error"]) == ("failed", "expired")
    since = mara.c.get("/v0/peer/outbox", params={"since": format_time(mara.clock[0])}, headers=H).json()["entries"]
    assert [x["id"] for x in since] == [r["id"]]
    assert mara.c.get("/v0/peer/outbox", params={"since": format_time(mara.clock[0] + 1)}, headers=H).json()["entries"] == []
    assert mara.c.get("/v0/peer/outbox", params={"since": "yesterday"}, headers=H).status_code == 400
    mara.clock[0] += 14 * 24 * 3600 + 1
    pump()
    assert all(x["id"] != r["id"] for x in mara.outbox())


def test_unfriend_is_delivered_after_forgetting_the_friend(mara, bob):
    befriend(mara, bob)
    mara.world.mode = "down"
    mara.send(bob, "unfriend", {})
    mara.post("/v0/peer/revoke", {"address": bob.address})
    mara.post("/v0/peer/forget", {"address": bob.address})
    mara.world.mode = "ok"
    mara.clock[0] += 31
    assert asyncio.run(mara.app.state.peers.pump()) == 1
    assert mara.outbox()[-1]["state"] == "delivered"


# --- keys changing ---


def test_key_change_without_rotation_is_refused_and_the_pin_kept(mara, bob):
    mara.pin(bob)
    old_fp = bob.fp
    bob.post("/v0/register", {"hub": bob.domain, "name": "bob", "invite": "i", "release": "0.9.0", "protocols": ["l0w/0.1"]})  # new keys, no chain
    bob.publish()
    assert bob.fp != old_fp
    r = mara.pin(bob)
    assert r.status_code == 409
    assert (r.json()["error"], r.json()["old_fingerprint"], r.json()["new_fingerprint"]) == ("key_changed", old_fp, bob.fp)
    assert messages.fingerprint(Key.from_dict(mara.peers()[bob.address]["sign_key"]).pub) == old_fp


def test_a_signed_rotation_is_followed_and_old_envelopes_stay_valid(mara, bob):
    befriend(mara, bob)
    old_fp = bob.fp
    assert bob.post("/v0/rotate", {"address": bob.address}).status_code == 200
    bob.publish()
    assert bob.fp != old_fp
    # bob writes with the new key before mara has looked: opening follows the signed rotation
    bob.send(mara, "message", {"thread": "t", "reply_to": None, "text": "rotated", "files": []})
    got = mara.open(bob.last_delivered())
    assert got.status_code == 200, got.text
    assert (got.json()["fingerprint"], got.json()["body"]["text"]) == (bob.fp, "rotated")
    p = mara.peers()[bob.address]
    assert len(p["old_keys"]) == 1 and messages.fingerprint(Key.from_dict(p["old_keys"][0]).pub) == old_fp
    assert mara.pin(bob).json()["rotated"] is False  # already followed
    # mara's token for bob still works for bob's old kid chain; and mara's messages still reach bob
    assert mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "ok", "files": []}).json()["state"] == "delivered"
    assert bob.open(mara.last_delivered()).status_code == 200


def test_pin_follows_a_rotation_and_never_rolls_the_seq_back(mara, bob):
    mara.pin(bob)
    old = dict(bob.world.entries[bob.address])
    bob.post("/v0/rotate", {"address": bob.address})
    bob.publish()
    r = mara.pin(bob)
    assert r.status_code == 200 and r.json()["rotated"] is True and r.json()["fingerprint"] == bob.fp
    bob.world.entries[bob.address] = old  # the directory serves a stale (older seq, old key) copy
    assert mara.pin(bob).json()["fingerprint"] == bob.fp
    assert mara.peers()[bob.address]["sign_seq"] == 2


def test_retired_seal_keys_are_kept_for_8_days(mara):
    peers = mara.app.state.peers
    old_kid = json.loads((mara.dir / "keys.json").read_text())["seal"]["pub"]["kid"]
    mara.post("/v0/register", {"hub": "a.example", "name": "mara", "invite": "i", "release": "0.9.0", "protocols": ["l0w/0.1"]})
    st = json.loads((mara.dir / "keys.json").read_text())
    assert st["seal"]["pub"]["kid"] != old_kid and [r["pub"]["kid"] for r in st["retired_seal"]] == [old_kid]
    assert old_kid in peers.seal_seeds()
    mara.clock[0] += 8 * 24 * 3600 + 1
    assert old_kid not in peers.seal_seeds() and st["seal"]["pub"]["kid"] in peers.seal_seeds()


# --- vetting ---


def test_vetting_refuses_private_and_internal_targets(monkeypatch):
    table = {"public.example": ["93.184.216.34"], "inner.example": ["10.1.2.3"], "loop.example": ["127.0.0.1"], "mixed.example": ["93.184.216.34", "192.168.0.9"]}
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: table.get(host, []))
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))
    assert safefetch.vet_url("https://public.example/x").ip == "93.184.216.34"
    for url in ("https://inner.example/", "https://loop.example/", "https://mixed.example/", "https://10.0.0.1/", "https://[::1]/", "https://169.254.169.254/",
                "http://public.example/", "https://control-hub/", "https://gateway:8780/", "https://tunnel/", "https://nowhere.example/", "https://u:p@public.example/"):
        with pytest.raises(safefetch.VetError):
            safefetch.vet_url(url)


def test_a_gateway_refuses_a_private_hub_but_dev_flags_allow_it(monkeypatch, tmp_path):
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: ["10.9.9.9"] if host == "hub.example" else [])
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))
    seen = []
    inner = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(404))

    async def get(net, url):
        return await net.request("GET", url)

    with pytest.raises(Exception, match="private network"):
        asyncio.run(get(Net(inner=inner, allow_http=False, connect_to=None), "https://hub.example/x"))
    with pytest.raises(Exception, match="only https"):
        asyncio.run(get(Net(inner=inner, allow_http=False, connect_to=None), "http://hub.example/x"))
    assert seen == []
    status, _, _ = asyncio.run(get(Net(inner=inner, allow_http=True, connect_to=None), "http://hub.example:8770/x"))
    assert status == 404 and str(seen[-1].url) == "http://hub.example:8770/x"
    asyncio.run(get(Net(inner=inner, allow_http=True, connect_to="127.0.0.1:9000"), "http://mara.hub.example/api/peer/v0/inbox"))
    assert seen[-1].url.host == "127.0.0.1" and seen[-1].url.port == 9000 and seen[-1].headers["host"] == "mara.hub.example"


def test_connect_to_can_route_each_hub_domain_to_its_own_address():
    seen = []
    inner = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={}))

    def get(net, url):
        return asyncio.run(net.request("GET", url))

    net = Net(inner=inner, allow_http=True, connect_to="hub-a.localhost=127.0.0.1:9001, hub-b.localhost=127.0.0.1:9002")
    for url in ("http://hub-b.localhost/.well-known/l0wagent/hub", "http://bob.hub-b.localhost:9002/api/peer/v0/inbox", "http://alice.hub-a.localhost:9001/x"):
        get(net, url)
    assert [(r.url.port, r.headers["host"]) for r in seen] == [(9002, "hub-b.localhost"), (9002, "bob.hub-b.localhost:9002"), (9001, "alice.hub-a.localhost:9001")]
    get(net, "http://hub-c.localhost/x")  # no entry and no default: the URL says where
    assert seen[-1].url.host == "hub-c.localhost"
    get(Net(inner=inner, allow_http=True, connect_to="hub-a.localhost=127.0.0.1:9001,127.0.0.1:9999"), "http://other.example/x")
    assert seen[-1].url.port == 9999 and seen[-1].headers["host"] == "other.example"


def test_the_first_retry_delay_is_settable(world, tmp_path):
    fast = Member(world, tmp_path, "fast", "a.example", backoff_first=2)
    bob = Member(world, tmp_path, "bob", "b.example")
    befriend(fast, bob)
    world.mode = "down"
    r = fast.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}).json()
    e = next(x for x in fast.outbox() if x["id"] == r["id"])
    assert e["state"] == "queued" and parse_time(e["next_at"]) - int(fast.clock[0]) == 2


def test_dev_flags_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("L0W_HUB_ALLOW_HTTP", "1")
    monkeypatch.setenv("L0W_HUB_CONNECT_TO", "127.0.0.1:8770")
    n = Net()
    assert (n.allow_http, n.connect_to, n.scheme, n.dev) == (True, "127.0.0.1:8770", "http", True)


def test_vetted_pin_connects_to_the_vetted_address(monkeypatch):
    monkeypatch.setattr(safefetch, "resolve", lambda host, port: ["93.184.216.34"] if host == "hub.example" else [])
    monkeypatch.setattr(safefetch, "_stack_cache", (0.0, set()))
    seen = []
    inner = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={}))
    status, _, _ = asyncio.run(Net(inner=inner, allow_http=False, connect_to=None).request("GET", "https://hub.example/.well-known/l0wagent/hub"))
    assert status == 200 and seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "hub.example"


def test_the_outbox_worker_runs_with_the_app(mara, bob):
    befriend(mara, bob)
    mara.world.mode = "down"
    r = mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}).json()
    mara.world.mode = "ok"
    mara.clock[0] += 31
    with TestClient(create_app(mara.dir, CT, TT, net=Net(transport=httpx.MockTransport(mara.world.handler), allow_http=False, connect_to=None),
                               clock=lambda: mara.clock[0], outbox_interval=0.05)):
        for _ in range(100):
            if next(x for x in mara.outbox() if x["id"] == r["id"])["state"] == "delivered":
                break
            time.sleep(0.05)
    assert next(x for x in mara.outbox() if x["id"] == r["id"])["state"] == "delivered"
