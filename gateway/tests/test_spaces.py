# ruff: noqa: F811
"""Shared spaces in the gateway (protocol/spaces.md): opening a space key, signing a space version."""

import copy

from l0wproto import b64, docs, spaces
from test_peer import H, Member, bob, mara, no_dev_env, world  # noqa: F401, F811  (fixtures)

SPACE = "sp-1"
K = bytes(range(32))


def space_key_doc(creator: Member, member: Member, *, epoch=1, kid=None, key=K, seal_pub=None):
    seal_kp = member.keypair("seal")
    kid = kid or seal_kp.kid
    sealed = spaces.seal_space_key(key, seal_pub or seal_kp.key, space=SPACE, epoch=epoch, member=member.address, member_kid=kid,
                                   granter=creator.address, granter_kid=creator.keypair("sign").kid)
    p = spaces.build_space_key(SPACE, epoch, member.address, kid, creator.address, creator.keypair("sign").kid, sealed)
    return docs.sign_document("space-key", p, [creator.keypair("sign")])


def open_key(m: Member, doc, token=None):
    return m.c.post("/v0/space/open-key", json={"doc": doc}, headers=H if token is None else token)


def test_open_key_returns_the_space_key(mara, bob):
    r = open_key(mara, space_key_doc(bob, mara))
    assert r.status_code == 200, r.text
    assert b64.decode(r.json()["key"]) == K


def test_open_key_with_a_retired_seal_key_that_is_still_kept(mara, bob):
    doc = space_key_doc(bob, mara)
    mara.post("/v0/register", {"hub": "a.example", "name": "mara", "invite": "i", "release": "0.9.0", "protocols": ["l0w/0.1"]})  # new keys
    r = open_key(mara, doc)
    assert r.status_code == 200 and b64.decode(r.json()["key"]) == K
    mara.clock[0] += 9 * 24 * 3600  # older than 8 days: no longer kept
    r = open_key(mara, doc)
    assert r.status_code == 403 and r.json()["error"] == "not_for_us"


def test_open_key_refuses_a_key_for_someone_else(mara, bob):
    doc = space_key_doc(bob, bob)  # bob's own key
    assert open_key(mara, doc).status_code == 403
    # member_kid that mara doesn't hold, sealed to bob's seal key but naming mara as member
    doc = space_key_doc(bob, mara, kid=bob.keypair("seal").kid, seal_pub=bob.keypair("seal").key)
    assert open_key(mara, doc).status_code == 403
    # names mara and her key id, but sealed to someone else's key: it just doesn't open
    doc = space_key_doc(bob, mara, seal_pub=bob.keypair("seal").key)
    r = open_key(mara, doc)
    assert r.status_code == 422 and r.json()["error"] == "cannot_open"


def test_open_key_builds_info_from_the_documents_own_fields(mara, bob):
    # tampering with any bound field (here: epoch, via a re-signed payload) means the info differs and nothing opens
    doc = space_key_doc(bob, mara)
    p = docs.parse_payload(doc)
    forged = docs.sign_document("space-key", {**p, "epoch": 2}, [bob.keypair("sign")])
    assert open_key(mara, forged).status_code == 422


def test_open_key_does_not_open_envelope_bodies(mara, bob):
    # an envelope-shaped or seal-info-shaped thing is not a space-key document
    assert open_key(mara, {"v": "l0w/0.1", "type": "envelope", "payload": "AA", "sigs": []}).status_code == 400
    for bad in (None, "x", [], {}, {"type": "space-key"}):
        assert open_key(mara, bad).status_code == 400, bad


def test_open_key_needs_keys_and_the_control_token(world, tmp_path, mara, bob):
    doc = space_key_doc(bob, mara)
    for h in ({}, {"Authorization": "Bearer tunnel-secret"}, {"Authorization": "Bearer nope"}):
        assert open_key(mara, doc, h).status_code == 401
        assert mara.c.post("/v0/sign/space-version", json={"payload": {}}, headers=h).status_code == 401


def payload_for(m: Member, **kw):
    meta = spaces.meta_seal(K, SPACE, 1, m.address, {"files": {}})
    base = dict(space=SPACE, seq=1, parent=None, epoch=1, author=m.address, blobs=[], meta=meta)
    base.update(kw)
    return spaces.build_space_version(base.pop("space"), base.pop("seq"), base.pop("parent"), base.pop("epoch"), base.pop("author"), base.pop("blobs"), base.pop("meta"))


def sign(m: Member, payload):
    return m.c.post("/v0/sign/space-version", json={"payload": payload}, headers=H)


def test_sign_space_version(mara):
    r = sign(mara, payload_for(mara))
    assert r.status_code == 200, r.text
    doc = r.json()["doc"]
    p = spaces.verify_space_version(doc, mara.keypair("sign").key, space=SPACE, author=mara.address)
    assert p["seq"] == 1 and p["parent"] is None
    assert spaces.valid_version_id(spaces.version_id_of(doc))


def test_sign_space_version_fills_in_header_fields(mara):
    p = payload_for(mara)
    r = sign(mara, {k: v for k, v in p.items() if k not in ("v", "type", "iat")})
    assert r.status_code == 200, r.text
    assert docs.parse_payload(r.json()["doc"])["iat"]


def test_sign_space_version_only_for_this_instance_as_author(mara, bob):
    assert sign(mara, payload_for(bob)).status_code == 403
    assert sign(mara, payload_for(mara, author="Mara@A.example")).status_code == 200  # same member, any case


def test_sign_space_version_rejects_bad_shapes(mara):
    good = payload_for(mara)
    for mut in ({"seq": 0}, {"seq": 2}, {"parent": "x"}, {"epoch": 0}, {"blobs": ["x"]}, {"meta": {"nonce": "AAAA", "ct": "AA"}}, {"type": "service"},
                {"v": "l0w/9"}, {"iat": "yesterday"}, {"extra": 1}, {"space": ""}):
        r = sign(mara, {**copy.deepcopy(good), **mut})
        assert r.status_code == 400, mut
    for bad in (None, "x", [], 3):
        assert mara.c.post("/v0/sign/space-version", json={"payload": bad}, headers=H).status_code == 400
    assert mara.c.post("/v0/sign/space-version", json={}, headers=H).status_code == 400


def test_sign_space_version_needs_keys(tmp_path):
    from starlette.testclient import TestClient

    from l0w_gateway import create_app

    c = TestClient(create_app(tmp_path / "d", "control-secret", "tunnel-secret"))
    r = c.post("/v0/sign/space-version", json={"payload": {"author": "a@b.example"}}, headers=H)
    assert r.status_code == 400  # shape is checked first
    p = spaces.build_space_version(SPACE, 1, None, 1, "abc@b.example", [], {"nonce": b64.encode(bytes(12)), "ct": "AA"})
    r = c.post("/v0/sign/space-version", json={"payload": p}, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "no_tunnel", r.text
    r = c.post("/v0/space/open-key", json={"doc": {"type": "space-key"}}, headers=H)
    assert r.status_code == 400


def key_payload(creator: Member, member: Member, **kw):
    seal_kp = member.keypair("seal")
    p = dict(space=SPACE, epoch=1, member=member.address, member_kid=seal_kp.kid, granter=creator.address, granter_kid=creator.keypair("sign").kid)
    p.update(kw)
    sealed = spaces.seal_space_key(K, seal_kp.key, space=p["space"], epoch=p["epoch"], member=p["member"], member_kid=p["member_kid"],
                                   granter=p["granter"], granter_kid=p["granter_kid"])
    return spaces.build_space_key(p["space"], p["epoch"], p["member"], p["member_kid"], p["granter"], p["granter_kid"], sealed)


def sign_key(m: Member, payload):
    return m.c.post("/v0/sign/space-key", json={"payload": payload}, headers=H)


def test_sign_space_key_as_the_granter_and_the_member_opens_it(mara, bob):
    r = sign_key(bob, key_payload(bob, mara))
    assert r.status_code == 200, r.text
    doc = r.json()["doc"]
    spaces.verify_space_key(doc, bob.keypair("sign").key, member=mara.address, granter=bob.address)
    r = open_key(mara, doc)
    assert r.status_code == 200 and b64.decode(r.json()["key"]) == K
    p = key_payload(bob, mara)
    r = sign_key(bob, {k: v for k, v in p.items() if k not in ("v", "type", "iat")})
    assert r.status_code == 200 and docs.parse_payload(r.json()["doc"])["iat"]


def test_sign_space_key_only_as_this_instance_with_its_current_key(mara, bob):
    assert sign_key(bob, key_payload(mara, bob)).status_code == 403  # mara as granter
    r = sign_key(bob, key_payload(bob, mara, granter_kid=mara.keypair("sign").kid))
    assert r.status_code == 403 and r.json()["error"] == "not_the_granter"
    good = key_payload(bob, mara)
    for mut in ({"extra": 1}, {"epoch": 0}, {"sealed": {"alg": "x"}}, {"member": "nope"}, {"type": "space-version"}):
        assert sign_key(bob, {**copy.deepcopy(good), **mut}).status_code == 400, mut
    for h in ({}, {"Authorization": "Bearer tunnel-secret"}):
        assert bob.c.post("/v0/sign/space-key", json={"payload": good}, headers=h).status_code == 401
