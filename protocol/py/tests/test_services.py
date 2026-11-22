"""Hub services (services.md): service and role-grant documents, the service envelope kinds, services_in."""

import copy
import hashlib
import json

import pytest

from conftest import ADDR, IAT, NOW
from l0wproto import b64, docs, keys, messages as M, payloads as P
from l0wproto.errors import ProtocolError, SignatureError, ValidationError

SVC = "board@hub.example"
VEC = __import__("pathlib").Path(__file__).resolve().parents[2] / "vectors"


@pytest.fixture()
def w(kp):
    svc = keys.from_seed(hashlib.sha256(b"svc-sign").digest(), "sign", IAT)
    svc_seal = keys.from_seed(hashlib.sha256(b"svc-seal").digest(), "seal", IAT)
    hub = kp["hub"]
    member = kp["sign"]
    mtok = docs.sign_document("delivery-token", P.build_delivery_token(ADDR, member.kid, SVC, svc.kid, iat=IAT), [member])
    stok = docs.sign_document("delivery-token", P.build_delivery_token(SVC, svc.kid, ADDR, member.kid, iat=IAT), [svc])
    grant = docs.sign_document("role-grant", P.build_role_grant(SVC, ADDR, member.kid, "moderator", iat=IAT), [svc])
    return dict(svc=svc, svc_seal=svc_seal, hub=hub, member=member, mtok=mtok, stok=stok, grant=grant, member_seal=kp["seal"])


def service_doc(w, signer=None, **kw):
    p = P.build_service(kw.pop("address", SVC), kw.pop("kind", "board"), kw.pop("roles", ["moderator", "poster"]), iat=IAT, **kw)
    return docs.sign_document("service", p, [signer or w["hub"]])


def send(w, kind, body, token="default", **kw):
    token = w["mtok"] if token == "default" else token
    return M.make_envelope(w["member"], w["member"].kid, ADDR, SVC, w["svc_seal"].key, kind, body, kw.pop("seq", 1), token, iat=IAT, **kw)


CALL = "AAAAAAAAAAAAAAAAAAAAAA"


# --- service documents ---------------------------------------------------------------------------


def test_service_round_trip(w):
    ext = {"git": "https://git.example/x.git", "commit": "a" * 40, "path": "extensions/board"}
    doc = service_doc(w, title="Board", description="d", docs="https://hub.example/s/board", extension=ext)
    p = P.verify_service(doc, w["hub"].key, hub_domain="hub.example", now=NOW)
    assert p["address"] == SVC and p["extension"] == ext and p["default"] == "on" and p["kind"] == "board"
    P.verify_service(service_doc(w, kind="custom", extension={**ext, "commit": "b" * 64}), w["hub"].key, now=NOW)


def test_service_wrong_signer_or_domain(w, kp):
    with pytest.raises(SignatureError):
        P.verify_service(service_doc(w, signer=kp["other"]), w["hub"].key, now=NOW)
    with pytest.raises(SignatureError):  # signed by the member's key, not the hub's
        P.verify_service(service_doc(w, signer=w["member"]), w["hub"].key, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_service(service_doc(w, address="board@other.example"), w["hub"].key, hub_domain="hub.example", now=NOW)


@pytest.mark.parametrize("kw", [
    {"kind": "forum"}, {"kind": ""}, {"roles": ["Mod"]}, {"roles": ["a b"]}, {"roles": ["x", "x"]},
    {"default": "maybe"}, {"docs": "http://x.example/"},
    {"extension": {"git": "http://git.example/x.git", "commit": "a" * 40, "path": "p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "A" * 40, "path": "p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "a" * 39, "path": "p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "main", "path": "p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "a" * 40, "path": "../p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "a" * 40, "path": "a/../p"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "a" * 40, "path": "/etc"}},
    {"extension": {"git": "https://git.example/x.git", "commit": "a" * 40, "path": ""}},
])
def test_service_bad_fields(w, kw):
    with pytest.raises(ValidationError):
        P.verify_service(service_doc(w, **kw), w["hub"].key, now=NOW)


def test_service_address_must_be_valid(w):
    with pytest.raises(ValidationError):
        P.verify_service(service_doc(w, address="Board"), w["hub"].key, now=NOW)


def caps(w, extras, **kw):
    return P.validate_hub(P.build_hub("H", "hub.example", [w["hub"].key], "https://hub.example/v0/", "wss://t/", extras=extras, iat=IAT, **kw))


def test_services_in_skips_bad(w, kp):
    good = service_doc(w)
    evil = service_doc(w, signer=kp["other"], address="relay@hub.example", kind="relay")
    foreign = service_doc(w, address="x1y@other.example")
    junk = copy.deepcopy(good)
    junk["payload"] = b64.encode(b'{"v":"l0w/0.1","type":"service"}')
    c = caps(w, [{"kind": "accelerator", "id": "cloudflared"}, {"kind": "service", "doc": good}, {"kind": "service", "doc": evil},
                 {"kind": "service", "doc": foreign}, {"kind": "service", "doc": junk}, {"kind": "service"},
                 {"kind": "service", "doc": good}])
    ok, skipped = P.services_in(c, now=NOW)
    assert [s["address"] for s in ok] == [SVC]
    assert len(skipped) == 5 and all(s.startswith("extras[") for s in skipped)
    ok2, sk2 = P.services_in(c, w["hub"].key, now=NOW)
    assert ok2 == ok and len(sk2) == 5
    assert P.services_in(caps(w, []), now=NOW) == ([], [])
    # a different hub key verifies nothing
    assert [x["address"] for x in P.services_in(c, kp["other"].key, now=NOW)[0]] == ["relay@hub.example"]


# --- role grants -----------------------------------------------------------------------------------


def test_role_grant(w, kp):
    g = w["grant"]
    p = P.verify_role_grant(g, w["svc"].key, service=SVC, holder=ADDR, holder_kid=w["member"].kid, now=NOW)
    assert p["role"] == "moderator"
    with pytest.raises(SignatureError):
        P.verify_role_grant(g, kp["other"].key, now=NOW)
    for kw in ({"service": "relay@hub.example"}, {"holder": "bob@hub.example"}, {"holder_kid": w["svc"].kid}):
        with pytest.raises(SignatureError):
            P.verify_role_grant(g, w["svc"].key, now=NOW, **kw)


def test_role_grant_exp_and_shape(w):
    exp = docs.sign_document("role-grant", P.build_role_grant(SVC, ADDR, w["member"].kid, "poster", iat=IAT, exp=NOW + 10), [w["svc"]])
    P.verify_role_grant(exp, w["svc"].key, now=NOW)
    with pytest.raises(ValidationError):
        P.verify_role_grant(exp, w["svc"].key, now=NOW + 11)
    for bad in (P.build_role_grant(SVC, ADDR, w["member"].kid, "Not A Role", iat=IAT),
                P.build_role_grant(SVC, ADDR, "k2.zzz", "poster", iat=IAT),
                P.build_role_grant("board", ADDR, w["member"].kid, "poster", iat=IAT)):
        with pytest.raises(ValidationError):
            P.verify_role_grant(docs.sign_document("role-grant", bad, [w["svc"]]), w["svc"].key, now=NOW)
    two = docs.sign_document("role-grant", P.build_role_grant(SVC, ADDR, w["member"].kid, "poster", iat=IAT), [w["svc"], w["member"]])
    with pytest.raises(SignatureError):
        P.verify_role_grant(two, w["svc"].key, now=NOW)


# --- envelope kinds ----------------------------------------------------------------------------------


def test_service_hello_is_tokenless(w):
    hello = {"token": w["mtok"], "release": "0.9.0"}
    doc = send(w, "service-hello", hello, token=None)
    assert "token" not in M.envelope_payload(doc)
    p, body = M.open_envelope(doc, my_address=SVC, my_seal_keys={w["svc_seal"].kid: w["svc_seal"].private},
                              sender_sign_key=w["member"].key, now=NOW)
    assert p["kind"] == "service-hello" and body == hello
    with pytest.raises(ValidationError):
        send(w, "service-hello", hello, token=w["mtok"])  # a token must be absent
    with pytest.raises(ValidationError):
        send(w, "service-call", {"call": CALL, "method": "m", "args": {}}, token=None)  # others need one
    with pytest.raises(ValidationError):
        send(w, "service-hello", {"token": w["mtok"]}, token=None)  # release missing
    with pytest.raises(ValidationError):
        send(w, "service-hello", {"release": "1"}, token=None)  # token missing


def test_service_welcome_and_bye(w):
    P.validate_body("service-welcome", {"token": w["stok"], "roles": [w["grant"]]})
    P.validate_body("service-welcome", {"token": w["stok"], "roles": []})
    for bad in ({"token": w["stok"]}, {"token": w["stok"], "roles": [{"type": "x"}]}, {"token": w["stok"], "roles": [w["stok"]]},
                {"roles": []}):
        with pytest.raises(ValidationError):
            P.validate_body("service-welcome", bad)
    send(w, "service-bye", {})


def test_service_call_body(w):
    good = {"call": CALL, "method": "post.create", "args": {"a": 1}, "role": w["grant"]}
    send(w, "service-call", good)
    send(w, "service-call", {**good, "role": None})
    send(w, "service-call", {k: v for k, v in good.items() if k != "role"})
    for bad in ({**good, "call": "short"}, {**good, "call": 5}, {**good, "method": ""}, {**good, "method": None}, {**good, "args": []},
                {**good, "args": None}, {**good, "role": {"type": "role-grant"}}, {**good, "role": "moderator"}):
        with pytest.raises(ValidationError) as e:
            P.validate_body("service-call", bad)
        assert e.value.code == "bad_body"
    big = {**good, "args": {"x": "a" * (1024 * 1024)}}
    with pytest.raises(ValidationError):
        P.validate_body("service-call", big)
    P.validate_body("service-call", {**good, "args": {"x": "a" * (1024 * 1024 - 2000)}})


def test_service_reply_body():
    P.validate_body("service-reply", {"call": CALL, "ok": True, "result": {"a": 1}})
    P.validate_body("service-reply", {"call": CALL, "ok": False, "error": {"code": "not_allowed", "detail": "x"}})
    P.validate_body("service-reply", {"call": CALL, "ok": False, "error": {"code": "not_allowed"}})
    for bad in ({"call": CALL, "ok": True}, {"call": CALL, "ok": True, "result": []}, {"call": CALL, "ok": "yes", "result": {}},
                {"call": CALL, "ok": False}, {"call": CALL, "ok": False, "error": {"detail": "x"}},
                {"call": CALL, "ok": False, "error": "nope"}, {"ok": True, "result": {}},
                {"call": CALL, "ok": True, "result": {"x": "a" * (1024 * 1024)}}):
        with pytest.raises(ValidationError):
            P.validate_body("service-reply", bad)


def test_service_event_body(w):
    P.validate_body("service-event", {"event": "post.new", "data": {"x": 1}})
    P.validate_body("service-event", {"event": "roles", "data": {"grants": [w["grant"]]}})
    P.validate_body("service-event", {"event": "roles", "data": {"grants": []}})
    for bad in ({"event": "roles", "data": {}}, {"event": "roles", "data": {"grants": [1]}}, {"event": "x", "data": []}, {"data": {}},
                {"event": 1, "data": {}}):
        with pytest.raises(ValidationError):
            P.validate_body("service-event", bad)


def test_old_api_still_works(w):
    assert "friend-request" in P.KINDS and "message" in P.KINDS and set(P.SERVICE_KINDS) <= set(P.KINDS)
    with pytest.raises(ProtocolError):
        P.validate_body("service-nope", {})


def test_service_call_vector():
    v = json.loads((VEC / "service-call.json").read_text())
    kp_ = keys.Key.from_dict(json.loads((VEC / "keys.json").read_text())["keys"]["member-sign"]["key"])
    seal_ = bytes.fromhex(v["recipient_seal_key"]["seed"])
    skey = keys.Key.from_dict(v["recipient_seal_key"]["key"])
    p, body = M.open_envelope(v["document"], my_address=v["recipient"], my_seal_keys={skey.kid: seal_}, sender_sign_key=kp_,
                              now=NOW)
    assert p["kind"] == "service-call" and json.loads(v["body_json"]) == body
    g = P.verify_role_grant(body["role"], keys.Key.from_dict(json.loads((VEC / "role-grant.json").read_text())["service_key"]["key"]),
                            service=v["recipient"], holder=ADDR, holder_kid=kp_.kid, now=NOW)
    assert g["role"] == "moderator"
