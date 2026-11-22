# ruff: noqa: F811
"""Hub services in the gateway (protocol/services.md): tokenless service-hello, service-* only from named services."""

import json

import pytest
from l0wproto import messages
from test_peer import H, Member, befriend, bob, mara, no_dev_env, world  # noqa: F401, F811  (fixtures)


@pytest.fixture
def svc(world, tmp_path):  # a service is a member with its own keys and directory entry
    return Member(world, tmp_path, "board", "a.example")


def set_services(m: Member, addresses):
    return m.c.put("/v0/peer/services", json={"addresses": addresses}, headers=H)


def hello(mara: Member, svc: Member):
    assert mara.pin(svc).status_code == 200
    r = mara.send(svc, "service-hello", {"release": "0.9.0"}, request_token=True)
    assert r.status_code == 200 and r.json()["state"] == "delivered", r.text
    return mara.last_delivered()


def test_services_list_is_stored_normalised_and_checked(mara):
    r = set_services(mara, ["Board@A.example", "relay@a.example", "board@a.example"])
    assert r.status_code == 200 and r.json() == {"addresses": ["board@a.example", "relay@a.example"]}
    assert json.loads((mara.dir / "services.json").read_text()) == ["board@a.example", "relay@a.example"]
    assert (mara.dir / "services.json").stat().st_mode & 0o777 == 0o600
    assert set_services(mara, "board@a.example").status_code == 400
    assert set_services(mara, ["nope"]).status_code == 400
    assert set_services(mara, []).json() == {"addresses": []}


def test_service_hello_needs_no_held_token_and_carries_ours(mara, svc):
    assert mara.pin(svc).status_code == 200
    assert mara.peers()[svc.address]["held_token"] is None
    r = mara.send(svc, "service-hello", {"release": "0.9.0"}, request_token=True)
    assert r.json()["state"] == "delivered", r.text
    env = messages.envelope_payload(mara.last_delivered())
    assert env["kind"] == "service-hello" and "token" not in env
    issued = mara.peers()[svc.address]["issued_token"]
    assert issued and not issued["revoked"]
    # an instance's gateway takes no service-hello (the hub's services do, and they aren't this gateway)
    assert svc.open(mara.last_delivered()).status_code == 403


def test_a_service_hello_without_a_token_in_its_body_gets_ours(mara, svc):
    mara.pin(svc)
    r = mara.send(svc, "service-hello", {"release": "0.9.0"})  # no request_token flag: still issued
    assert r.json()["state"] == "delivered"
    sent = mara.peers()[svc.address]["issued_token"]["doc"]
    assert sent["type"] == "delivery-token"


def test_request_token_is_refused_for_other_kinds(mara, bob):
    mara.pin(bob)
    r = mara.send(bob, "message", {"thread": "t", "reply_to": None, "text": "x", "files": []}, request_token=True)
    assert r.status_code == 400


def test_service_welcome_from_a_named_service_stores_its_token(mara, svc):
    hello(mara, svc)
    assert mara.peers()[svc.address]["held_token"] is None
    # the service pins mara (holding the token from her hello) and answers with its own token for her
    assert svc.pin(mara, held_token=mara.peers()[svc.address]["issued_token"]["doc"]).status_code == 200
    mine = svc.post("/v0/peer/token", {"address": mara.address}).json()["token"]
    r = svc.send(mara, "service-welcome", {"token": mine, "roles": []})
    assert r.json()["state"] == "delivered", r.text
    env = svc.last_delivered()
    # until control names the service, the gateway takes nothing service-shaped from it
    assert mara.open(env).status_code == 403
    assert set_services(mara, [svc.address]).status_code == 200
    got = mara.open(env)
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "service-welcome"
    assert mara.peers()[svc.address]["held_token"] == mine
    # now calls go out with that token
    r = mara.send(svc, "service-call", {"call": "AAAAAAAAAAAAAAAAAAAAAA", "method": "x", "args": {}, "role": None})
    assert r.json()["state"] == "delivered"


def test_service_kinds_from_a_friend_are_refused(mara, bob):
    befriend(mara, bob)
    r = bob.send(mara, "service-event", {"event": "ping", "data": {}})
    assert r.json()["state"] == "delivered"
    assert mara.open(bob.last_delivered()).status_code == 403
    # and once bob is named a service, his ordinary messages are refused instead
    set_services(mara, [bob.address])
    assert mara.open(bob.last_delivered()).status_code == 200
    bob.send(mara, "message", {"thread": "t", "reply_to": None, "text": "hi", "files": []})
    assert mara.open(bob.last_delivered()).status_code == 403


def test_service_welcome_whose_token_is_not_the_services_is_refused(mara, bob, svc):
    hello(mara, svc)
    set_services(mara, [svc.address])
    svc.pin(mara, held_token=mara.peers()[svc.address]["issued_token"]["doc"])
    # a token issued by someone else (bob, to mara) in the welcome
    bob.pin(mara)
    stray = bob.post("/v0/peer/token", {"address": mara.address}).json()["token"]
    svc.send(mara, "service-welcome", {"token": stray, "roles": []})
    assert mara.open(svc.last_delivered()).status_code == 400
    assert mara.peers()[svc.address]["held_token"] is None
