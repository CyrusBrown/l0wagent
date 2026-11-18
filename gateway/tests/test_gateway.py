import json
import os
import stat

import pytest
from l0wproto import b64, docs, hopsig, keys, payloads
from l0wproto.keys import Key
from starlette.testclient import TestClient

from l0w_gateway import create_app

CT, TT = "control-secret", "tunnel-secret"
HUB = "hub.example"
ADDR = f"mara@{HUB}"
HOSTS = {"instance": "mara.hub.example", "files": "mara-files.hub.example"}
REG = {"hub": HUB, "name": "mara", "invite": "inv-1", "mode": "tunnel", "own_urls": None, "release": "0.9.0", "protocols": ["l0w/0.1"]}


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def dd(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def client(dd):
    return TestClient(create_app(dd, CT, TT))


def c_post(client, path, body, token=CT):
    return client.post(path, json=body, headers=hdr(token))


def register(client):
    r = c_post(client, "/v0/register", REG)
    assert r.status_code == 200, r.text
    return r.json()


def self_body(seq=1):
    return {"address": ADDR, "seq": seq, "hostnames": HOSTS, "own_urls": None, "protocols": ["l0w/0.1"]}


def test_healthz_no_auth(client):
    assert client.get("/healthz").json() == {"ok": True}


CONTROL = [("GET", "/v0/keys"), ("POST", "/v0/register"), ("POST", "/v0/sign/self"), ("POST", "/v0/sign/leave"), ("POST", "/v0/sign/moved"),
           ("POST", "/v0/rotate"), ("POST", "/v0/recover"), ("POST", "/v0/hop-sign"), ("PUT", "/v0/tunnel"), ("GET", "/v0/tunnel/status")]
TUNNEL = [("GET", "/v0/tunnel/config"), ("POST", "/v0/tunnel/hello"), ("PUT", "/v0/tunnel/status"), ("POST", "/v0/tunnel/accelerator")]


@pytest.mark.parametrize(("method", "path"), CONTROL)
def test_control_endpoints_reject_other_tokens(client, method, path):
    for h in ({}, hdr(TT), hdr("nope"), {"Authorization": CT}):
        r = client.request(method, path, json={}, headers=h)
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"


@pytest.mark.parametrize(("method", "path"), TUNNEL)
def test_tunnel_endpoints_reject_other_tokens(client, method, path):
    for h in ({}, hdr(CT), hdr("nope")):
        r = client.request(method, path, json={}, headers=h)
        assert r.status_code == 401


def test_startup_refuses_bad_tokens(dd):
    for a, b in (("", "x"), ("x", ""), ("x", "x"), ("", "")):
        with pytest.raises(ValueError):
            create_app(dd, a, b)


def test_no_keys(client):
    assert client.get("/v0/keys", headers=hdr(CT)).json() == {"keys": [], "history": []}
    for path, body in (("/v0/sign/self", self_body()), ("/v0/rotate", {"address": ADDR}), ("/v0/sign/leave", {"address": ADDR, "moved_to": None})):
        r = c_post(client, path, body)
        assert r.status_code == 409 and r.json()["error"] == "no_keys"


def test_bad_input(client):
    assert c_post(client, "/v0/register", {"hub": HUB}).status_code == 400
    r = client.post("/v0/register", content=b"{nope", headers=hdr(CT))
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_register(client, dd):
    out = register(client)
    reg = payloads.verify_registration(out["registration"], HUB)
    assert reg["name"] == "mara" and reg["invite"] == "inv-1"
    assert [k["use"] for k in out["keys"]] == ["sign", "seal", "recover"]
    assert reg["keys"] == out["keys"]
    rk = out["recovery_key"]
    assert rk.startswith("l0w-recovery:")
    seed = rk.split(":", 1)[1]
    for f in dd.iterdir():
        text = f.read_text()
        assert seed not in text and "recover_seed" not in text
    stored = json.loads((dd / "keys.json").read_text())
    assert "seed" not in stored["recover"]
    assert client.get("/v0/keys", headers=hdr(CT)).json() == {"keys": out["keys"], "history": []}
    assert keys.from_seed(b64.decode(seed), "recover").kid == out["keys"][2]["kid"]


def test_register_again_makes_fresh_keys(client):
    a = register(client)
    b = register(client)
    assert a["keys"][0]["kid"] != b["keys"][0]["kid"]


def test_keys_file_mode_and_retired(client, dd):
    register(client)
    register(client)
    p = dd / "keys.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    st = json.loads(p.read_text())
    assert len(st["retired"]) == 3 and all("seed" not in k for k in st["retired"])
    assert st["history"] == []


def test_sign_self(client):
    out = register(client)
    r = c_post(client, "/v0/sign/self", self_body())
    assert r.status_code == 200
    p = payloads.verify_self(r.json()["doc"])
    assert p["keys"] == out["keys"] and p["address"] == ADDR and p["seq"] == 1 and p["hostnames"] == HOSTS
    assert "own_urls" not in p or p["own_urls"] is None
    own = {"instance": "https://a.example", "files": "https://f.example"}
    r = c_post(client, "/v0/sign/self", {**self_body(2), "own_urls": own})
    assert payloads.verify_self(r.json()["doc"])["own_urls"] == own


def test_sign_self_ignores_caller_keys(client):
    out = register(client)
    r = c_post(client, "/v0/sign/self", {**self_body(), "keys": [keys.generate("sign").key.to_dict()]})
    assert payloads.verify_self(r.json()["doc"])["keys"] == out["keys"]


def test_rotate(client, dd):
    out = register(client)
    old = Key.from_dict(out["keys"][0])
    r = c_post(client, "/v0/rotate", {"address": ADDR})
    assert r.status_code == 200
    rot = r.json()
    new_keys = rot["keys"]
    assert new_keys[0]["kid"] != old.kid and new_keys[1:] == out["keys"][1:]
    p = payloads.verify_rotation(rot["rotation"], old)
    assert p["new"] == new_keys[0] and p["reason"] == "scheduled"
    st = json.loads((dd / "keys.json").read_text())
    assert st["retired"] == [out["keys"][0]] and st["history"] == [rot["rotation"]]
    s = payloads.verify_self(c_post(client, "/v0/sign/self", self_body(2)).json()["doc"])
    assert s["keys"] == new_keys
    assert payloads.verify_rotation(s["history"][0], old)["new"]["kid"] == new_keys[0]["kid"]
    assert client.get("/v0/keys", headers=hdr(CT)).json()["history"] == [rot["rotation"]]


def test_recover(client):
    out = register(client)
    old = Key.from_dict(out["keys"][0])
    rec = Key.from_dict(out["keys"][2])
    bad = "l0w-recovery:" + b64.encode(os.urandom(32))
    for wrong in (bad, "garbage", "l0w-recovery:!!", "l0w-recovery:" + b64.encode(b"short")):
        r = c_post(client, "/v0/recover", {"address": ADDR, "recovery_key": wrong})
        assert r.status_code == 403 and r.json()["error"] == "wrong_recovery_key"
    assert client.get("/v0/keys", headers=hdr(CT)).json()["history"] == []
    r = c_post(client, "/v0/recover", {"address": ADDR, "recovery_key": out["recovery_key"]})
    assert r.status_code == 200
    rot = r.json()
    p = payloads.verify_rotation(rot["rotation"], old, rec)
    assert p["reason"] == "compromise" and p["new"] == rot["keys"][0]
    _, signer = docs.verify_one_of(rot["rotation"], [old, rec])
    assert signer == rec.kid
    assert payloads.verify_self(c_post(client, "/v0/sign/self", self_body(3)).json()["doc"])["keys"] == rot["keys"]


def test_leave_and_moved(client):
    out = register(client)
    sign = Key.from_dict(out["keys"][0])
    d = c_post(client, "/v0/sign/leave", {"address": ADDR, "moved_to": "mara@other.example"}).json()["doc"]
    assert payloads.verify_leave(d, sign)["moved_to"] == "mara@other.example"
    d = c_post(client, "/v0/sign/leave", {"address": ADDR, "moved_to": None}).json()["doc"]
    assert payloads.verify_leave(d, sign)["moved_to"] is None
    d = c_post(client, "/v0/sign/moved", {"from": ADDR, "to": "mara@other.example"}).json()["doc"]
    assert payloads.verify_moved(d, sign)["to"] == "mara@other.example"


def test_hop_sign(client):
    out = register(client)
    sign = Key.from_dict(out["keys"][0])
    uri = f"https://{HUB}/v0/members/mara/leave"
    body = b'{"a":1}'
    r = c_post(client, "/v0/hop-sign", {"method": "POST", "url": uri, "body": b64.encode(body), "content_type": "application/json", "address": ADDR})
    h = dict(r.json()["headers"])
    h["Content-Type"] = "application/json"
    res = hopsig.verify_request("POST", uri, h, body, lambda a, k: sign if (a, k) == (ADDR, sign.kid) else None, hopsig.InMemoryNonceCache())
    assert res.address == ADDR
    r = c_post(client, "/v0/hop-sign", {"method": "GET", "url": uri, "body": "", "content_type": None, "address": ADDR})
    h = r.json()["headers"]
    hopsig.verify_request("GET", uri, h, b"", lambda a, k: sign, hopsig.InMemoryNonceCache())
    assert c_post(client, "/v0/hop-sign", {"method": "POST", "url": uri, "body": b64.encode(body), "content_type": None, "address": ADDR}).status_code == 400


TUNNEL_CFG = {"url": "wss://hub.example/v0/tunnel", "address": ADDR, "hub": HUB, "hostnames": HOSTS, "release": "0.9.0", "connect_to": None,
              "accelerator": {"enabled": False, "api": None}}


def test_tunnel_config_and_hello(client):
    assert client.get("/v0/tunnel/config", headers=hdr(TT)).json() == {"config": None}
    nonce = b64.encode(os.urandom(32))
    r = c_post(client, "/v0/tunnel/hello", {"nonce": nonce, "hub": HUB}, TT)
    assert r.status_code == 409 and r.json()["error"] == "no_tunnel"
    out = register(client)
    assert client.put("/v0/tunnel", json={"config": TUNNEL_CFG}, headers=hdr(CT)).json() == {"config": TUNNEL_CFG}
    assert client.get("/v0/tunnel/config", headers=hdr(TT)).json() == {"config": TUNNEL_CFG}
    r = c_post(client, "/v0/tunnel/hello", {"nonce": nonce, "hub": HUB}, TT)
    assert r.status_code == 200
    p = payloads.verify_tunnel_hello(r.json()["doc"], Key.from_dict(out["keys"][0]), nonce=nonce, hub=HUB)
    assert p["address"] == ADDR and p["release"] == "0.9.0"
    r = c_post(client, "/v0/tunnel/hello", {"nonce": nonce, "hub": "evil.example"}, TT)
    assert r.status_code == 403 and r.json()["error"] == "wrong_hub"
    assert client.put("/v0/tunnel", json={"config": None}, headers=hdr(CT)).json() == {"config": None}
    assert client.get("/v0/tunnel/config", headers=hdr(TT)).json() == {"config": None}


def test_tunnel_status(client):
    assert client.get("/v0/tunnel/status", headers=hdr(CT)).json() == {"config": False, "status": None}
    client.put("/v0/tunnel", json={"config": TUNNEL_CFG}, headers=hdr(CT))
    st = {"state": "connected", "since": "2026-11-17T21:30:00Z", "error": None, "session": "s1", "close_code": None}
    assert client.put("/v0/tunnel/status", json=st, headers=hdr(TT)).json() == {"ok": True}
    assert client.get("/v0/tunnel/status", headers=hdr(CT)).json() == {"config": True, "status": st}
    assert client.put("/v0/tunnel/status", json={"state": "weird"}, headers=hdr(TT)).status_code == 400
    assert client.put("/v0/tunnel", json={"config": {"url": "x"}}, headers=hdr(CT)).status_code == 400


def test_tunnel_config_accelerator(client):
    base = {k: v for k, v in TUNNEL_CFG.items() if k != "accelerator"}  # absent: the default
    assert client.put("/v0/tunnel", json={"config": base}, headers=hdr(CT)).json()["config"]["accelerator"] == {"enabled": False, "api": None}
    acc = {"enabled": True, "api": "https://hub.example/v0/accelerators/cloudflared"}
    assert client.put("/v0/tunnel", json={"config": {**base, "accelerator": acc}}, headers=hdr(CT)).status_code == 200
    assert client.get("/v0/tunnel/config", headers=hdr(TT)).json()["config"]["accelerator"] == acc
    for bad in ({"enabled": "yes", "api": None}, {"enabled": True, "api": "ftp://x"}, {"enabled": True, "api": "nonsense"}, {"enabled": True, "api": 5}, "on"):
        assert client.put("/v0/tunnel", json={"config": {**base, "accelerator": bad}}, headers=hdr(CT)).status_code == 400


def test_tunnel_accelerator_request(client):
    r = c_post(client, "/v0/tunnel/accelerator", {"action": "enable", "service": "http://127.0.0.1:8790"}, TT)
    assert r.status_code == 409 and r.json()["error"] == "no_tunnel"
    out = register(client)
    key = Key.from_dict(out["keys"][0])
    client.put("/v0/tunnel", json={"config": TUNNEL_CFG}, headers=hdr(CT))
    r = c_post(client, "/v0/tunnel/accelerator", {"action": "enable", "service": "http://127.0.0.1:8790"}, TT)
    assert r.status_code == 200
    p = payloads.verify_accelerator_request(r.json()["doc"], key, hub=HUB, address=ADDR)
    assert p["action"] == "enable" and p["service"] == "http://127.0.0.1:8790" and p["accelerator"] == "cloudflared" and p["hub"] == HUB
    # rapid calls: iat strictly increases
    iats = []
    for a in ("activate", "deactivate", "disable"):
        d = c_post(client, "/v0/tunnel/accelerator", {"action": a, "service": "http://127.0.0.1:1"}, TT).json()["doc"]
        p = payloads.verify_accelerator_request(d, key, hub=HUB, address=ADDR)
        assert p["action"] == a and "service" not in p
        iats.append(p["iat"])  # ISO strings sort in time order
    assert iats == sorted(set(iats))
    for body in ({"action": "nuke"}, {"action": "enable"}, {"action": "enable", "service": "http://evil.example:80"}, {"action": "enable", "service": "http://127.0.0.1:99999"}):
        assert c_post(client, "/v0/tunnel/accelerator", body, TT).status_code == 400


def test_tunnel_status_accelerator(client):
    client.put("/v0/tunnel", json={"config": TUNNEL_CFG}, headers=hdr(CT))
    st = {"state": "connected", "since": "2026-11-17T21:30:00Z", "error": None, "session": "s1", "close_code": None}
    acc = {"state": "active", "since": "2026-11-17T21:31:00Z", "error": None}
    assert client.put("/v0/tunnel/status", json={**st, "accelerator": acc}, headers=hdr(TT)).json() == {"ok": True}
    assert client.get("/v0/tunnel/status", headers=hdr(CT)).json()["status"] == {**st, "accelerator": acc}
    client.put("/v0/tunnel/status", json={**st, "state": "offline"}, headers=hdr(TT))  # silent about the accelerator: kept
    assert client.get("/v0/tunnel/status", headers=hdr(CT)).json()["status"]["accelerator"] == acc
    assert client.put("/v0/tunnel/status", json={**st, "accelerator": {"state": "weird"}}, headers=hdr(TT)).status_code == 400


def test_state_survives_restart(dd):
    c1 = TestClient(create_app(dd, CT, TT))
    out = register(c1)
    c1.put("/v0/tunnel", json={"config": TUNNEL_CFG}, headers=hdr(CT))
    rot = c_post(c1, "/v0/rotate", {"address": ADDR}).json()
    c2 = TestClient(create_app(dd, CT, TT))
    got = c2.get("/v0/keys", headers=hdr(CT)).json()
    assert got == {"keys": rot["keys"], "history": [rot["rotation"]]}
    assert got["keys"][1:] == out["keys"][1:]
    assert c2.get("/v0/tunnel/config", headers=hdr(TT)).json() == {"config": TUNNEL_CFG}
    payloads.verify_self(c_post(c2, "/v0/sign/self", self_body(5)).json()["doc"])
    for f in dd.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
