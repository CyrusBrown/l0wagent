"""A fake hub and gateway for the hub-services tests (test_hubservices.py, and the ext.hub tests in test_extensions.py).

The fake gateway answers what control calls (gateway/README.md) and signs nothing itself: the service's keys and the hub's
are real l0wproto keys made here, so the service documents in the capabilities and the role grants in a welcome are really
signed and really verified. `/v0/peer/open` takes the "envelope" as JSON describing what a real gateway would have verified
and opened, as in test_friends.py."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from l0wproto import b64, docs, keys, payloads, spaces
from l0wproto.errors import ProtocolError

from l0w_control.app import create_app
from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env
from l0w_control.membership import Gateway

PW = "correct horse battery"
TUNNEL_IP = ("10.255.254.9", 40000)
TUNNEL_NET = "10.255.254.0/24"
AGENT = {"X-Agent-Token": "agent-secret"}
HUB = "hub.example"
ME = f"mara@{HUB}"
ECHO = f"echo@{HUB}"
BOARD = f"board@{HUB}"
KID = "k1." + "A" * 22
TOKEN = {"v": "l0w/0.1", "type": "delivery-token", "payload": "x", "sigs": []}


@dataclass
class FakeGateway:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)
    errors: dict[str, tuple[int, str, str]] = field(default_factory=dict)  # path -> (status, error, detail), for the next call
    sign_keys: dict[str, keys.KeyPair] = field(default_factory=dict)  # service address -> the key its directory entry has
    seal_keys: dict[str, keys.KeyPair] = field(default_factory=dict)  # member address -> its seal key (made on first pin)
    send_state: str = "queued"
    n: int = 0
    hostnames: dict[str, dict] = field(default_factory=dict)  # address -> the instance/files hostnames its directory entry has (default x/y)
    me_sign: keys.KeyPair = field(default_factory=lambda: keys.generate("sign"))  # this instance's keys, which only the gateway holds
    me_seal: keys.KeyPair = field(default_factory=lambda: keys.generate("seal"))

    def bodies(self, path: str, kind: str | None = None) -> list[dict]:
        return [b for _, p, b in self.calls if p == path and b is not None and (kind is None or b.get("kind") == kind)]

    def sent(self, kind: str) -> list[dict]:
        return self.bodies("/v0/peer/send", kind)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw = request.content
        body = json.loads(raw) if raw and path != "/v0/peer/open" else None
        self.calls.append((request.method, path, body if path != "/v0/peer/open" else None))
        if path in self.errors:
            status, error, detail = self.errors.pop(path)
            return httpx.Response(status, json={"error": error, "detail": detail})
        if path == "/v0/peer/open":
            return httpx.Response(200, json=json.loads(raw))
        if path == "/v0/peer/pin":
            kp = self.sign_keys[body["address"]]
            return httpx.Response(200, json={"address": body["address"], "fingerprint": "7Q2M-9KXA-4D1F-R0VE", "rotated": False, "sign_key": kp.key.to_dict(),
                                             "seal_key": self.seal_keys.setdefault(body["address"], keys.generate("seal")).key.to_dict(),
                                             "hostnames": self.hostnames.get(body["address"], {"instance": "x", "files": "y"})})
        if path == "/v0/space/open-key":  # as the real gateway: info from the document's own fields, our seal key
            try:
                p = spaces.space_key_payload(body["doc"])
                return httpx.Response(200, json={"key": b64.encode(spaces.open_space_key_payload(self.me_seal.private, p))})
            except ProtocolError as e:
                return httpx.Response(422, json={"error": "cannot_open", "detail": e.detail})
        if path == "/v0/sign/space-version":
            try:
                p = {"v": "l0w/0.1", "type": "space-version", "iat": "2026-11-22T10:00:00Z", **body["payload"]}
                spaces.validate_space_version(p)
            except ProtocolError as e:
                return httpx.Response(400, json={"error": "bad_request", "detail": e.detail})
            if p["author"] != ME:
                return httpx.Response(403, json={"error": "not_the_author", "detail": "this instance signs only its own versions"})
            return httpx.Response(200, json={"doc": docs.sign_document("space-version", p, [self.me_sign])})
        if path == "/v0/sign/space-key":
            try:
                p = {"v": "l0w/0.1", "type": "space-key", "iat": "2026-11-22T10:00:00Z", **body["payload"]}
                spaces.validate_space_key(p)
            except ProtocolError as e:
                return httpx.Response(400, json={"error": "bad_request", "detail": e.detail})
            if p["granter"] != ME or p["granter_kid"] != self.me_sign.kid:
                return httpx.Response(403, json={"error": "not_the_granter", "detail": "this instance signs only keys it grants"})
            return httpx.Response(200, json={"doc": docs.sign_document("space-key", p, [self.me_sign])})
        if path == "/v0/peer/send":
            self.n += 1
            return httpx.Response(200, json={"id": f"out-{self.n}", "state": self.send_state, "error": "refused by them" if self.send_state == "failed" else None})
        return httpx.Response(200, json={"ok": True})


class World:
    def __init__(self, be: SqliteBackend, c: TestClient, t: TestClient, gw: FakeGateway, hub: keys.KeyPair, svc: dict[str, keys.KeyPair]):
        self.be, self.c, self.t, self.gw, self.hub, self.svc = be, c, t, gw, hub, svc
        self.n = 0

    @property
    def hs(self):
        return self.be.hubservices_

    def run(self, coro, timeout: float = 20):
        return self.be._await(coro, timeout=timeout)

    def service_doc(self, address: str, *, kind: str = "custom", roles=("tester",), default: str = "on", title: str | None = None, sign_with: keys.KeyPair | None = None) -> dict:
        p = payloads.build_service(address, kind, list(roles), title=title or address.split("@")[0].title(), description="A service.", default=default)
        return {"kind": "service", "doc": docs.sign_document("service", p, [sign_with or self.hub])}

    def set_caps(self, *extras: dict) -> None:
        caps = {"domain": HUB, "extras": list(extras)}
        self.be.db.ex("UPDATE hub_membership SET capabilities=? WHERE id=1", (json.dumps(caps),))

    def sync(self, **kw) -> None:
        self.run(self.hs.sync(**kw))

    def arrive(self, sender: str, kind: str, body: dict, env_id: str | None = None):
        self.n += 1
        opened = {"from": sender, "from_kid": "k1.x", "fingerprint": "7Q2M-9KXA-4D1F-R0VE", "id": env_id or f"env-{self.n}", "seq": self.n, "kind": kind,
                  "iat": "2026-11-22T10:00:00Z", "body": body}
        return self.t.post("/api/peer/v0/inbox", content=json.dumps(opened).encode())

    def grant(self, role: str = "tester", service: str = ECHO, *, sign_with: keys.KeyPair | None = None, holder: str = ME, exp: str | None = None) -> dict:
        p = payloads.build_role_grant(service, holder, KID, role, exp=exp)
        return docs.sign_document("role-grant", p, [sign_with or self.svc[service]])

    def welcome(self, address: str = ECHO, grants: list | None = None):
        return self.arrive(address, "service-welcome", {"token": TOKEN, "roles": grants if grants is not None else [self.grant(service=address)]})

    def row(self, address: str = ECHO) -> dict:
        return self.be.db.q1("SELECT * FROM hub_services WHERE address=?", (address,))  # type: ignore[return-value]

    def notes(self) -> list[dict]:
        return self.be.db.q("SELECT * FROM notifications WHERE kind='hub' ORDER BY id")

    def connect_echo(self, grants: list | None = None) -> None:
        self.sync()
        assert self.row()["state"] == "connecting"
        assert self.welcome(grants=grants).status_code == 202
        assert self.row()["state"] == "connected"

    def start_call(self, *args, **kw):
        """A call running on the app's loop: (concurrent future, wait-for-the-send function)."""
        fut = asyncio.run_coroutine_threadsafe(self.hs.call(*args, **kw), self.be.loop)

        def sent(n: int = 1) -> list[dict]:
            end = time.time() + 5
            while time.time() < end:
                got = self.gw.sent("service-call")
                if len(got) >= n:
                    return got
                time.sleep(0.02)
            raise AssertionError("no service-call was sent")

        return fut, sent

    def reply(self, call_id: str, **kw) -> None:
        body = {"call": call_id, **kw}
        assert self.arrive(ECHO, "service-reply", body).status_code == 202


@pytest.fixture
def hw(tmp_path, monkeypatch):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    monkeypatch.setenv("L0W_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("L0W_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("L0W_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    monkeypatch.setenv("L0W_TUNNEL_SUBNET", TUNNEL_NET)
    (tmp_path / "extensions").mkdir()
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", tunnel_subnet=TUNNEL_NET,
              runner_url="http://127.0.0.1:9", egress_url="http://127.0.0.1:9", agent_home=tmp_path / "agent-home")
    be = SqliteBackend(env=env)
    gw = FakeGateway()
    hub = keys.generate("sign")
    svc = {ECHO: keys.generate("sign"), BOARD: keys.generate("sign")}
    gw.sign_keys.update(svc)
    be.membership_.gw = Gateway("http://gateway", "tok", transport=httpx.MockTransport(gw.handler))
    be.db.ex("INSERT INTO hub_membership(id, state, hub_base, hub_domain, hub_name, hub_key, capabilities, api, name, address, mode, hostnames, tunnel_url, joined_at)"
             " VALUES(1,'joined','https://hub.example',?,'Hub',?,'{}','x','mara',?,'tunnel',?,'wss://t','2026-11-18T00:00:00Z')",
             (HUB, json.dumps(hub.key.to_dict()), ME, json.dumps({"instance": "mara.hub.example", "files": "files--mara.hub.example"})))
    app = create_app(be)
    with TestClient(app, client=("192.168.1.20", 5000), headers={"x-l0w-client": "test"}) as c:
        c.post("/api/setup/owner", json={"username": "mara", "password": PW, "timezone": "UTC"})
        c.post("/api/auth/login", json={"username": "mara", "password": PW})
        w = World(be, c, TestClient(app, client=TUNNEL_IP), gw, hub, svc)
        w.set_caps(w.service_doc(ECHO), w.service_doc(BOARD, kind="board", roles=("moderator", "poster"), default="off"))
        yield w


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def agent(w: World, method: str, path: str, **kw):
    return w.c.request(method, f"/api/agent{path}", headers=AGENT, **kw)


def wait_sent(w: World, n: int = 1) -> list[dict]:
    """The service-calls sent so far, once there are at least n."""
    end = time.time() + 5
    while time.time() < end:
        got = w.gw.sent("service-call")
        if len(got) >= n:
            return got
        time.sleep(0.02)
    raise AssertionError("no service-call was sent")
