import pytest

from conftest import ADDR, HUB, IAT, NOW
from l0wproto import b64, docs, payloads as P
from l0wproto.errors import SignatureError, ValidationError

HN = {"instance": "mara.hub.example", "files": "files--mara.hubfiles.example"}


def member_keys(kp):
    return [kp["sign"].key, kp["seal"].key, kp["recover"].key]


def test_address():
    assert P.split_address("Mara@Hub.Example") == ("mara", "hub.example")
    for bad in ("mara", "a@b@c", "ab@hub.example", "mara@", "mara@-x", "Ma_ra@hub.example"):
        with pytest.raises(ValidationError):
            P.split_address(bad)


def test_self_roundtrip_and_rules(kp):
    p = P.build_self(ADDR, 3, member_keys(kp), HN, iat=IAT)
    d = docs.sign_document("self", p, [kp["sign"]])
    assert P.verify_self(d, now=NOW)["seq"] == 3
    # signed by a key that's not the payload's signing key
    with pytest.raises(SignatureError):
        P.verify_self(docs.sign_document("self", p, [kp["other"]]), now=NOW)
    # two signing keys: ambiguous
    p2 = P.build_self(ADDR, 3, member_keys(kp) + [kp["sign2"].key], HN, iat=IAT)
    with pytest.raises(ValidationError):
        P.verify_self(docs.sign_document("self", p2, [kp["sign"]]), now=NOW)
    # missing recovery key
    p3 = P.build_self(ADDR, 3, member_keys(kp)[:2], HN, iat=IAT)
    with pytest.raises(ValidationError):
        P.validate_self(p3)
    # extra signature
    with pytest.raises(SignatureError):
        P.verify_self(docs.sign_document("self", p, [kp["sign"], kp["recover"]]), now=NOW)


def test_self_own_urls_and_extra_fields(kp):
    p = P.build_self(ADDR, 0, member_keys(kp), HN, own_urls=None, iat=IAT)
    p["future_field"] = [1]
    P.validate_self(p)
    p["own_urls"] = {"instance": "https://x"}
    with pytest.raises(ValidationError):
        P.validate_self(p)


def test_self_kid_mismatch_in_keys(kp):
    p = P.build_self(ADDR, 1, member_keys(kp), HN, iat=IAT)
    p["keys"][0]["kid"] = kp["other"].kid
    with pytest.raises(Exception):
        P.validate_self(p)


def test_bool_not_int(kp):
    p = P.build_self(ADDR, True, member_keys(kp), HN, iat=IAT)
    with pytest.raises(ValidationError):
        P.validate_self(p)


def test_rotation(kp):
    p = P.build_rotation(ADDR, kp["sign"].kid, kp["sign2"].key, "scheduled", iat=IAT)
    P.verify_rotation(docs.sign_document("rotation", p, [kp["sign"]]), kp["sign"].key, kp["recover"].key, now=NOW)
    P.verify_rotation(docs.sign_document("rotation", p, [kp["recover"]]), kp["sign"].key, kp["recover"].key, now=NOW)
    with pytest.raises(SignatureError):
        P.verify_rotation(docs.sign_document("rotation", p, [kp["other"]]), kp["sign"].key, kp["recover"].key, now=NOW)
    with pytest.raises(SignatureError):  # new key can't approve its own rotation
        P.verify_rotation(docs.sign_document("rotation", p, [kp["sign2"]]), kp["sign"].key, kp["recover"].key, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_rotation(docs.sign_document("rotation", p, [kp["sign"]]), kp["other"].key, kp["sign"].key, now=NOW)
    bad = P.build_rotation(ADDR, kp["sign"].kid, kp["sign2"].key, "whatever", iat=IAT)
    with pytest.raises(ValidationError):
        P.verify_rotation(docs.sign_document("rotation", bad, [kp["sign"]]), kp["sign"].key, now=NOW)


def test_recovery_key_rotation(kp):
    new = kp["other"]
    p = P.build_rotation(ADDR, kp["recover"].kid, new.key, "scheduled", iat=IAT)
    P.verify_rotation(docs.sign_document("rotation", p, [kp["recover"]]), kp["recover"].key, now=NOW)


def test_revocation(kp):
    p = P.build_revocation(ADDR, kp["sign"].kid, "stolen", iat=IAT)
    d = docs.sign_document("revocation", p, [kp["recover"]])
    P.verify_revocation(d, [kp["recover"].key, kp["hub"].key], now=NOW)
    with pytest.raises(SignatureError):
        P.verify_revocation(d, [kp["hub"].key], now=NOW)
    with pytest.raises(SignatureError):  # the compromised key can't revoke
        P.verify_revocation(docs.sign_document("revocation", p, [kp["sign"]]), [kp["recover"].key], now=NOW)


def test_moved_leave_reregistration(kp):
    d = docs.sign_document("moved", P.build_moved(ADDR, "mara@other.example", iat=IAT), [kp["sign"]])
    P.verify_moved(d, kp["sign"].key, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_moved(docs.sign_document("moved", P.build_moved(ADDR, "nope", iat=IAT), [kp["sign"]]), kp["sign"].key, now=NOW)
    for to in (None, "mara@other.example"):
        P.verify_leave(docs.sign_document("leave", P.build_leave(ADDR, to, iat=IAT), [kp["sign"]]), kp["sign"].key, now=NOW)
    p = P.build_leave(ADDR, iat=IAT)
    del p["moved_to"]
    with pytest.raises(ValidationError):
        P.validate_leave(p)
    r = docs.sign_document("reregistration", P.build_reregistration(ADDR, ["k1.a"], ["k1.b"], iat=IAT), [kp["hub"]])
    P.verify_reregistration(r, kp["hub"].key, now=NOW)
    with pytest.raises(SignatureError):
        P.verify_reregistration(r, kp["other"].key, now=NOW)


def hub_doc(kp, **over):
    p = P.build_hub("Example", HUB, [kp["hub"].key], f"https://{HUB}/v0/", "wss://tunnel.hub.example/v0/tunnel", iat=IAT,
                    extras=[{"kind": "future-thing", "x": 1}])
    p.update(over)
    return docs.sign_document("hub", p, [kp["hub"]])


def test_hub(kp):
    p = P.verify_hub(hub_doc(kp), pinned=kp["hub"].key, now=NOW)
    assert p["domain"] == HUB
    with pytest.raises(SignatureError):  # pinned key not in the document
        P.verify_hub(hub_doc(kp), pinned=kp["other"].key, now=NOW)
    with pytest.raises(SignatureError):  # signed by a key not listed
        P.verify_hub(docs.sign_document("hub", P.build_hub("x", HUB, [kp["hub"].key], f"https://{HUB}/v0/", "wss://t", iat=IAT),
                                        [kp["other"]]), now=NOW)
    with pytest.raises(ValidationError):
        P.verify_hub(hub_doc(kp, api=f"https://{HUB}/v0"), now=NOW)  # no trailing slash
    m = {"invites": True, "signups": "closed", "own_url": True, "require_2fa": False}
    with pytest.raises(ValidationError):
        P.verify_hub(hub_doc(kp, membership=m), now=NOW)
    with pytest.raises(ValidationError):
        P.verify_hub(hub_doc(kp, membership={**m, "require_2fa": True, "signups": "maybe"}), now=NOW)


def reg(kp, signers=("sign", "recover"), **kw):
    p = P.build_registration(HUB, "mara", "code", "tunnel", member_keys(kp), iat=kw.pop("iat", IAT), **kw)
    return docs.sign_document("registration", p, [kp[s] for s in signers])


def test_registration(kp):
    assert P.verify_registration(reg(kp), HUB, now=NOW)["name"] == "mara"
    with pytest.raises(SignatureError):
        P.verify_registration(reg(kp, signers=("sign",)), HUB, now=NOW)
    with pytest.raises(SignatureError):
        P.verify_registration(reg(kp, signers=("sign", "other")), HUB, now=NOW)
    with pytest.raises(SignatureError):  # seal key can't sign; also not required
        P.verify_registration(reg(kp, signers=("recover", "sign2")), HUB, now=NOW)
    with pytest.raises(ValidationError):  # replay at another hub
        P.verify_registration(reg(kp), "other.example", now=NOW)
    with pytest.raises(ValidationError):  # stale iat
        P.verify_registration(reg(kp), HUB, now=NOW + 301)
    P.verify_registration(reg(kp), HUB, now=NOW + 300)
    with pytest.raises(ValidationError):
        P.verify_registration(reg(kp, own_urls={"instance": "https://a", "files": "https://b"}), HUB, now=NOW)


def test_registration_own_url_and_reregister(kp):
    ou = {"instance": "https://box.mara.example", "files": "https://files.mara.example"}
    p = P.build_registration(HUB, "mara", "c", "own-url", member_keys(kp), own_urls=ou, reregister=True, iat=IAT)
    d = docs.sign_document("registration", p, [kp["sign"], kp["recover"]])
    assert P.verify_registration(d, HUB, now=NOW)["reregister"] is True
    p["own_urls"] = {"instance": "http://box", "files": "https://f"}
    with pytest.raises(ValidationError):
        P.validate_registration(p, HUB, NOW)
    p = P.build_registration(HUB, "Bad_Name", "c", "tunnel", member_keys(kp), iat=IAT)
    with pytest.raises(ValidationError):
        P.validate_registration(p, HUB, NOW)


def test_tunnel_hello(kp):
    n = b64.encode(bytes(32))
    d = docs.sign_document("tunnel-hello", P.build_tunnel_hello(ADDR, n, HUB, iat=IAT), [kp["sign"]])
    P.verify_tunnel_hello(d, kp["sign"].key, nonce=n, hub=HUB, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_tunnel_hello(d, kp["sign"].key, nonce=b64.encode(b"\x01" * 32), hub=HUB, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_tunnel_hello(d, kp["sign"].key, nonce=n, hub="other.example", now=NOW)
    with pytest.raises(ValidationError):
        P.verify_tunnel_hello(d, kp["sign"].key, nonce=n, hub=HUB, now=NOW + 301)
    with pytest.raises(SignatureError):
        P.verify_tunnel_hello(d, kp["other"].key, nonce=n, hub=HUB, now=NOW)
    short = docs.sign_document("tunnel-hello", P.build_tunnel_hello(ADDR, b64.encode(b"x"), HUB, iat=IAT), [kp["sign"]])
    with pytest.raises(ValidationError):
        P.verify_tunnel_hello(short, kp["sign"].key, nonce=b64.encode(b"x"), hub=HUB, now=NOW)


def test_a_document_is_not_another_type(kp):
    d = docs.sign_document("leave", P.build_leave(ADDR, iat=IAT), [kp["sign"]])
    with pytest.raises(Exception):
        P.verify_moved(d, kp["sign"].key, now=NOW)
    d["type"] = "moved"
    with pytest.raises(SignatureError):
        P.verify_moved(d, kp["sign"].key, now=NOW)
