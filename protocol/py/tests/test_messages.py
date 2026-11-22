import copy
import hashlib
import json

import pytest

from conftest import ADDR, IAT, NOW
from l0wproto import b64, docs, keys, messages as M, payloads as P, seal as S
from l0wproto.consts import ENVELOPE_MAX_AGE, MAX_FILES
from l0wproto.errors import ProtocolError, SealError, SignatureError, ValidationError
from l0wproto.times import format_time

BOB = "bob@other.example"


@pytest.fixture()
def world(kp):
    bob_sign = keys.from_seed(hashlib.sha256(b"bob-sign").digest(), "sign", IAT)
    bob_seal = keys.from_seed(hashlib.sha256(b"bob-seal").digest(), "seal", IAT)
    mara = kp["sign"]
    # bob's token for mara, and mara's token for bob
    tok = docs.sign_document("delivery-token", P.build_delivery_token(BOB, bob_sign.kid, ADDR, mara.kid, iat=IAT), [bob_sign])
    req_tok = docs.sign_document("delivery-token", P.build_delivery_token(ADDR, mara.kid, BOB, bob_sign.kid, iat=IAT), [mara])
    return dict(mara=mara, bob_sign=bob_sign, bob_seal=bob_seal, tok=tok, req_tok=req_tok)


def msg_body(**kw):
    return {"thread": "t1", "reply_to": None, "text": "hi", "files": [], **kw}


def send(w, kind="message", body=None, token="default", **kw):
    token = w["tok"] if token == "default" else token
    return M.make_envelope(w["mara"], w["mara"].kid, ADDR, BOB, w["bob_seal"].key, kind, body if body is not None else msg_body(),
                           kw.pop("seq", 1), token, iat=kw.pop("iat", IAT), **kw)


def receive(w, doc, now=NOW, **kw):
    return M.open_envelope(doc, my_address=BOB, my_seal_keys={w["bob_seal"].kid: w["bob_seal"].private},
                           sender_sign_key=kw.pop("sender_sign_key", w["mara"].key), now=now)


def test_round_trip(world):
    doc = send(world, body=msg_body(text="Hello é"))
    p, body = receive(world, doc)
    assert body["text"] == "Hello é" and p["kind"] == "message" and p["seq"] == 1
    assert M.token_id(doc) == M.token_id(world["tok"]) == json.loads(b64.decode(world["tok"]["payload"]))["id"]
    # the plaintext never appears in the outer document
    assert b"Hello" not in b64.decode(doc["payload"])
    tp = M.verify_delivery_token(world["tok"], world["bob_sign"].key, world["mara"].kid, now=NOW)
    assert tp["holder"] == ADDR
    with pytest.raises(SignatureError):
        M.verify_delivery_token(world["tok"], world["bob_sign"].key, "k1.AAAAAAAAAAAAAAAAAAAAAA", now=NOW)
    with pytest.raises(SignatureError):
        M.verify_delivery_token(world["tok"], world["mara"].key, now=NOW)


def test_wrong_recipient_key_and_unknown_kid(world, kp):
    doc = send(world)
    other = keys.from_seed(hashlib.sha256(b"x").digest(), "seal", IAT)
    with pytest.raises(SealError) as e:
        M.open_envelope(doc, my_address=BOB, my_seal_keys={world["bob_seal"].kid: other.private},
                        sender_sign_key=world["mara"].key, now=NOW)
    assert e.value.code == "bad_seal"
    with pytest.raises(SealError):
        M.open_envelope(doc, my_address=BOB, my_seal_keys={}, sender_sign_key=world["mara"].key, now=NOW)


def test_wrong_address_is_not_here(world):
    with pytest.raises(ProtocolError) as e:
        M.open_envelope(send(world), my_address="carol@other.example", my_seal_keys={}, sender_sign_key=world["mara"].key, now=NOW)
    assert e.value.code == "not_here"


def test_resigned_by_another_sender_fails(world, kp):
    """Eve takes Mara's sealed body and wraps it in her own envelope: the info binds Mara, so it does not open."""
    doc = send(world)
    p = json.loads(b64.decode(doc["payload"]))
    eve = kp["other"]
    p2 = {**p, "from": "eve@hub.example", "from_kid": eve.kid}
    forged = docs.sign_document("envelope", p2, [eve])
    with pytest.raises(SealError):
        receive(world, forged, sender_sign_key=eve.key)
    # same address and id but Eve's key: still bound by from_kid
    p3 = {**p, "from_kid": eve.kid}
    with pytest.raises(SealError):
        receive(world, docs.sign_document("envelope", p3, [eve]), sender_sign_key=eve.key)
    # and a tampered signature or the wrong key resolved for from_kid
    with pytest.raises(SignatureError):
        receive(world, doc, sender_sign_key=eve.key)
    bad = copy.deepcopy(doc)
    bad["sigs"][0]["sig"] = b64.encode(bytes(64))
    with pytest.raises(SignatureError):
        receive(world, bad)


def test_tampered_sealed_or_id_fails(world):
    doc = send(world)
    p = json.loads(b64.decode(doc["payload"]))
    ct = bytearray(b64.decode(p["sealed"]["ct"]))
    ct[0] ^= 1
    p["sealed"]["ct"] = b64.encode(bytes(ct))
    with pytest.raises(SealError):
        receive(world, docs.sign_document("envelope", p, [world["mara"]]))
    p = json.loads(b64.decode(doc["payload"]))
    p["id"] = P.new_id()  # id is bound into info
    with pytest.raises(SealError):
        receive(world, docs.sign_document("envelope", p, [world["mara"]]))
    p = json.loads(b64.decode(doc["payload"]))
    p["sealed"]["alg"] = "HPKE-v2"
    with pytest.raises(SealError):
        receive(world, docs.sign_document("envelope", p, [world["mara"]]))


def test_token_rules(world):
    # tokenless non-friend-request: refused at build and at validation
    with pytest.raises(ValidationError):
        send(world, token=None)
    # friend-request with a token: refused
    fr = {"code": None, "note": "hi", "by": "owner", "token": world["req_tok"]}
    with pytest.raises(ValidationError):
        send(world, "friend-request", fr, token=world["tok"])
    doc = send(world, "friend-request", fr, token=None)
    p, body = receive(world, doc)
    assert "token" not in p and M.token_id(doc) is None and body["by"] == "owner"
    # a token that is not a delivery-token
    with pytest.raises(ValidationError):
        send(world, token=docs.sign_document("same-owner", P.build_same_owner(ADDR, "k1." + "A" * 22, BOB, "k1." + "B" * 22, iat=IAT),
                                             [world["mara"]]))
    # incoming: a token slipped into a friend request after signing is rejected on parse
    p = json.loads(b64.decode(doc["payload"]))
    p["token"] = world["tok"]
    with pytest.raises(ValidationError):
        M.envelope_payload(docs.sign_document("envelope", p, [world["mara"]]))


def test_iat_window(world):
    old = send(world, iat=NOW - ENVELOPE_MAX_AGE - 1)
    with pytest.raises(ValidationError):
        receive(world, old)
    receive(world, send(world, iat=NOW - ENVELOPE_MAX_AGE))
    with pytest.raises(ValidationError):
        receive(world, send(world, iat=NOW + 301))
    receive(world, send(world, iat=NOW + 300))
    # iat is made-at: the same bytes opened 7 days later still pass
    receive(world, send(world), now=NOW + 7 * 86400)


def test_unknown_kind(world):
    doc = send(world)
    p = json.loads(b64.decode(doc["payload"]))
    p["kind"] = "carrier-pigeon"
    with pytest.raises(ValidationError) as e:
        receive(world, docs.sign_document("envelope", p, [world["mara"]]))
    assert e.value.code == "unknown_kind"
    with pytest.raises(ValidationError) as e:
        P.validate_body("carrier-pigeon", {})
    assert e.value.code == "unknown_kind"


def test_bad_body_on_open(world):
    """A sealed body that does not match its kind is bad_body (a sender that skipped validation)."""
    doc = send(world)
    p = json.loads(b64.decode(doc["payload"]))
    info = S.seal_info(p["from"], p["from_kid"], p["to"], p["to_kid"], p["id"])
    for plain in (b'{"nope":1}', b"not json", b"[1]"):
        p["sealed"] = S.seal(world["bob_seal"].key.pub, plain, info)
        with pytest.raises(ValidationError) as e:
            receive(world, docs.sign_document("envelope", p, [world["mara"]]))
        assert e.value.code == "bad_body"


def file(name="a.txt", data=b"abc"):
    return P.build_file(name, "text/plain", data)


def test_body_validation_per_kind(world):
    tok = world["tok"]
    ok = {
        "friend-request": {"code": "abc", "note": "n", "by": "assistant", "token": tok},
        "friend-accept": {"request": "r", "token": tok},
        "friend-decline": {"request": "r"},
        "unfriend": {},
        "message": msg_body(reply_to="x", files=[file()]),
        "presence": {"state": "idle", "next": "tonight", "now": None},
        "wake-request": {"reason": "r"},
        "same-owner": {"statement": docs.sign_document("same-owner", P.build_same_owner(ADDR, "k1." + "A" * 22, BOB, "k1." + "B" * 22, iat=IAT),
                                                         [world["mara"]])},
        "wake-reply": {"request": "r", "outcome": "later"},
    }
    assert set(ok) == set(P.KINDS) - set(P.SERVICE_KINDS)  # the service kinds: test_services.py
    for k, b in ok.items():
        assert P.validate_body(k, b) is b
        with pytest.raises(ValidationError) as e:
            P.validate_body(k, [b])
        assert e.value.code == "bad_body"
    P.validate_body("presence", {"state": "asleep", "next": "2026-11-18T08:00:00Z", "now": "x"})
    P.validate_body("presence", {"state": "asleep"})
    bad = [
        ("friend-request", {**ok["friend-request"], "by": "robot"}),
        ("friend-request", {k: v for k, v in ok["friend-request"].items() if k != "code"}),
        ("friend-request", {**ok["friend-request"], "token": {"type": "x"}}),
        ("friend-request", {**ok["friend-request"], "note": "x" * 5000}),
        ("friend-accept", {"request": "r"}),
        ("friend-decline", {}),
        ("message", msg_body(text=5)),
        ("message", msg_body(files="x")),
        ("message", msg_body(reply_to=3)),
        ("message", {"text": "x"}),
        ("message", msg_body(files=[{**file(), "size": 99}])),
        ("message", msg_body(files=[{**file(), "sha256": "0" * 64}])),
        ("message", msg_body(files=[file("../x")])),
        ("message", msg_body(files=[file("a/b")])),
        ("presence", {"state": "dead"}),
        ("presence", {"state": "idle", "next": "someday"}),
        ("wake-request", {}),
        ("wake-reply", {"request": "r", "outcome": "maybe"}),
        ("same-owner", {"statement": tok}),
        ("same-owner", {}),
    ]
    for k, b in bad:
        with pytest.raises(ValidationError):
            P.validate_body(k, b)


def test_file_totals():
    big = b"\x00" * (3 * 1024 * 1024)
    P.validate_body("message", msg_body(files=[file("a", big)]))
    with pytest.raises(ValidationError):
        P.validate_body("message", msg_body(files=[file("a", big), file("b", big)]))
    assert MAX_FILES == 5 * 1024 * 1024


def test_same_owner_two_signatures(kp):
    a, b = kp["sign"], kp["sign2"]
    p = P.build_same_owner(ADDR, a.kid, "mara2@other.example", b.kid, iat=IAT)
    d = docs.sign_document("same-owner", p, [a, b])
    assert P.verify_same_owner(d, a.key, b.key, NOW)["b"] == "mara2@other.example"
    for one in ([a], [b]):
        with pytest.raises(SignatureError):
            P.verify_same_owner(docs.sign_document("same-owner", p, one), a.key, b.key, NOW)
    with pytest.raises(ValidationError):
        P.validate_same_owner(P.build_same_owner(ADDR, a.kid, ADDR, b.kid, iat=IAT))


def test_envelope_shape_rules(world):
    p = json.loads(b64.decode(send(world)["payload"]))
    P.validate_envelope(p)
    for mut in ({"seq": 0}, {"id": "short"}, {"to": "nope"}, {"to_kid": "x"}, {"sealed": {"alg": "x"}}, {"kind": 3}):
        with pytest.raises(ValidationError):
            P.validate_envelope({**p, **mut})
    with pytest.raises(ValidationError):
        P.validate_envelope({k: v for k, v in p.items() if k != "token"})


def test_size_limit(world):
    # files are base64'd into the body, the body is sealed and base64'd, and the payload is base64'd again:
    # about 2.4x, so the full 5 MiB of files fits in the 16 MiB envelope
    doc = send(world, body=msg_body(files=[file("a", b"\x01" * (5 * 1024 * 1024))]))
    assert len(json.dumps(doc)) < 16 * 1024 * 1024
    p, body = receive(world, doc)
    assert body["files"][0]["size"] == 5 * 1024 * 1024
    with pytest.raises(ValidationError):  # over the files limit
        send(world, body=msg_body(files=[file("a", b"\x01" * (5 * 1024 * 1024 + 1))]))


# --- fingerprints and friend codes -----------------------------------------------------------


def test_fingerprint():
    fp = M.fingerprint(bytes(32))
    assert len(fp) == 19 and fp.count("-") == 3
    assert set(fp.replace("-", "")) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    n = int.from_bytes(hashlib.sha256(bytes(32)).digest()[:10], "big")
    assert fp.replace("-", "") == "".join("0123456789ABCDEFGHJKMNPQRSTVWXYZ"[(n >> s) & 31] for s in range(75, -1, -5))
    assert M.fingerprint(b"a" * 32) != fp


def test_friend_code_round_trip_and_rejections():
    fp = M.fingerprint(b"k" * 32)
    text, secret = M.make_friend_code("Bob@Other.Example", fp)
    assert len(secret) == 16 and text.startswith("l0wf1:bob@other.example:") and text.count(":") == 3
    assert M.parse_friend_code(text) == ("bob@other.example", fp, secret)
    text2, _ = M.make_friend_code("bob@other.example:8443", fp.replace("-", ""), secret)
    assert M.parse_friend_code(text2) == ("bob@other.example:8443", fp, secret)
    flat = fp.replace("-", "")
    sec = b64.encode(secret)
    for bad in ("", "x", text.replace("l0wf1", "l0wf2"), text[:-1], text + "A", f"l0wf1:bob@other.example:{flat}",
                f"l0wf1:bob@other.example:{flat[:-1]}:{sec}", f"l0wf1:bob@other.example:{fp}:{sec}",
                f"l0wf1:bob@other.example:U{flat[1:]}:{sec}", f"l0wf1:bob@other.example:{flat.lower()}:{sec}",
                f"l0wf1:BOB@other.example:{flat}:{sec}", f"l0wf1:bob:{flat}:{sec}", f"l0wf1:bob@other.example:{flat}:{sec}=",
                f"l0wf1:bob@other.example:{flat}:{b64.encode(b'x' * 15)}", f"l0wf1::{flat}:{sec}"):
        with pytest.raises(ValidationError) as e:
            M.parse_friend_code(bad)
        assert e.value.code == "bad_friend_code", bad
    with pytest.raises(ValidationError):
        M.make_friend_code("bob@other.example", "nope")
    with pytest.raises(ValidationError):
        M.make_friend_code("bob@other.example", fp, b"short")


# --- HPKE ------------------------------------------------------------------------------------

H = bytes.fromhex


def test_hpke_rfc9180_a2_1():
    info = H("4f6465206f6e2061204772656369616e2055726e")
    sk_e, pk_e = S.derive_key_pair(H("909a9b35d3dc4713a5e72a4da274b55d3d3821a37e5d099e74a647db583a904b"))
    sk_r, pk_r = S.derive_key_pair(H("1ac01f181fdf9f352797655161c58b75c656a6cc2716dcb66372da835542e1df"))
    assert pk_e.hex() == "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a"
    assert sk_e.hex() == "f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600"
    assert pk_r.hex() == "4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a"
    assert sk_r.hex() == "8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb"
    shared = S._shared_secret(S._dh(sk_e, pk_r), pk_e, pk_r)
    assert shared.hex() == "0bbe78490412b4bbea4812666f7916932b828bba79942424abb65244930d69a7"
    key, nonce = S._key_schedule(shared, info)
    assert key.hex() == "ad2744de8e17f4ebba575b3f5f5a8fa1f69c2a07f6e7500bc60ca6e3e3ec1c91"
    assert nonce.hex() == "5c4d98150661b848853b547f"
    pt, aad = b"Beauty is truth, truth beauty", b"Count-0"
    enc, ct = S.hpke_seal(pk_r, info, aad, pt, eph_priv=sk_e)
    assert enc == pk_e
    assert ct.hex() == "1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938e131f0def8c8b60b4db21993c62ce81883d2dd1b51a28"
    assert S.hpke_open(sk_r, enc, info, aad, ct) == pt


def test_hpke_interop_with_cryptography():
    hpke = pytest.importorskip("cryptography.hazmat.primitives.hpke")
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    suite = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
    sk = hashlib.sha256(b"interop").digest()
    priv = X25519PrivateKey.from_private_bytes(sk)
    pub = S._pub(sk)
    info = b"some info"
    # ours seals, theirs opens
    sealed = S.seal(pub, b"from us", info)
    assert suite.decrypt(b64.decode(sealed["enc"]) + b64.decode(sealed["ct"]), priv, info=info) == b"from us"
    # theirs seals (enc || ct), ours opens
    blob = suite.encrypt(b"from them", priv.public_key(), info=info)
    assert S.open_sealed(sk, {"alg": S.ALG, "enc": b64.encode(blob[:32]), "ct": b64.encode(blob[32:])}, info) == b"from them"


def test_seal_failures():
    sk = hashlib.sha256(b"s").digest()
    pub = S._pub(sk)
    s = S.seal(pub, b"x", b"i")
    assert S.open_sealed(sk, s, b"i") == b"x"
    for bad in (None, {}, {**s, "alg": "other"}, {**s, "enc": "AA"}, {**s, "ct": ""}, {**s, "enc": 5}, "x"):
        with pytest.raises(SealError):
            S.open_sealed(sk, bad, b"i")
    with pytest.raises(SealError):
        S.open_sealed(sk, s, b"other info")
    with pytest.raises(SealError):
        S.open_sealed(hashlib.sha256(b"t").digest(), s, b"i")
    with pytest.raises(SealError):
        S.seal(b"short", b"x", b"i")
    assert S.seal_info("a@h", "k1.a", "b@h", "k1.b", "id") == b"l0wagent-seal/1\na@h\nk1.a\nb@h\nk1.b\nid"


# --- vectors ---------------------------------------------------------------------------------

VEC = __import__("pathlib").Path(__file__).resolve().parents[2] / "vectors"


def vec(name):
    return json.loads((VEC / f"{name}.json").read_text())


def test_envelope_vector_opens():
    v = vec("envelope")
    now = __import__("l0wproto.times", fromlist=["x"]).parse_time(v["now"])
    sender = keys.Key.from_dict(v["verify_keys"][0])
    seal_key = keys.Key.from_dict(v["recipient_seal_key"]["key"])
    priv = bytes.fromhex(v["recipient_seal_key"]["seed"])
    assert keys.from_seed(priv, "seal", seal_key.created).key == seal_key
    p, body = M.open_envelope(v["document"], my_address=v["recipient"], my_seal_keys={seal_key.kid: priv}, sender_sign_key=sender, now=now)
    assert docs.serialize_payload(body).decode() == v["body_json"]
    assert S.seal_info(p["from"], p["from_kid"], p["to"], p["to_kid"], p["id"]).decode() == v["sealed_info"]
    assert S.seal_info(p["from"], p["from_kid"], p["to"], p["to_kid"], p["id"]).hex() == v["sealed_info_hex"]
    # the ephemeral seed reproduces enc
    assert b64.encode(S._pub(bytes.fromhex(v["ephemeral_seed"]))) == p["sealed"]["enc"]
    # the embedded token is the token vector
    assert p["token"] == vec("delivery-token")["document"]


def test_token_and_same_owner_vectors():
    v = vec("delivery-token")
    issuer = keys.Key.from_dict(v["verify_keys"][0])
    now = __import__("l0wproto.times", fromlist=["x"]).parse_time(v["now"])
    p = M.verify_delivery_token(v["document"], issuer, now=now)
    assert json.loads(v["payload_json"]) == p and M.token_id(v["document"]) == p["id"]
    s = vec("same-owner")
    ka, kb = (keys.Key.from_dict(k) for k in s["verify_keys"])
    P.verify_same_owner(s["document"], ka, kb, now)


def test_fingerprint_and_friend_code_vectors():
    for c in vec("fingerprint")["cases"]:
        raw = b64.decode(c["pub"])
        assert hashlib.sha256(raw).hexdigest() == c["sha256_hex"] and M.fingerprint(raw) == c["fingerprint"]
    fc = vec("friend-code")
    for c in fc["valid"]:
        a, fp, sec = M.parse_friend_code(c["text"])
        assert (a, fp, b64.encode(sec)) == (c["address"], c["fingerprint"], c["secret"])
        assert M.make_friend_code(a, fp, sec)[0] == c["text"]
    for t in fc["invalid"]:
        with pytest.raises(ValidationError):
            M.parse_friend_code(t)


def test_schemas_validate_vectors():
    import jsonschema
    from referencing import Registry, Resource

    reg = Registry()
    for f in (VEC.parent / "schemas").glob("*.json"):
        s = json.loads(f.read_text())
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))

    def val(n):
        return jsonschema.Draft202012Validator(json.loads((VEC.parent / "schemas" / f"{n}.json").read_text()), registry=reg)

    for n in ("envelope", "delivery-token", "same-owner"):
        v = vec(n)
        val("signed-document").validate(v["document"])
        val(n).validate(json.loads(v["payload_json"]))
    env = json.loads(vec("envelope")["payload_json"])
    ev = val("envelope")
    assert not ev.is_valid({k: x for k, x in env.items() if k != "token"})
    assert not ev.is_valid({**env, "kind": "friend-request"})  # a token with a friend request
    assert ev.is_valid({**{k: x for k, x in env.items() if k != "token"}, "kind": "friend-request"})
    assert not ev.is_valid({**env, "seq": 0})
