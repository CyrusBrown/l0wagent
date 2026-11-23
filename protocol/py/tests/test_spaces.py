"""Shared spaces (spaces.md): subkeys, blobs, sealed manifests, locks, space-key and space-version documents."""

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from conftest import ADDR, IAT, NOW
from l0wproto import b64, docs, keys, seal, spaces as S
from l0wproto.errors import SealError, SignatureError, ValidationError

PROTO = Path(__file__).resolve().parents[2]
V = json.loads((PROTO / "vectors" / "spaces.json").read_text())
K = bytes.fromhex(V["key_hex"])
SPACE = V["space"]


def schema(name):
    reg = Registry()
    for p in (PROTO / "schemas").glob("*.json"):
        s = json.loads(p.read_text())
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))
    s = json.loads((PROTO / "schemas" / f"{name}.json").read_text())
    return jsonschema.Draft202012Validator(s, registry=reg, format_checker=jsonschema.FormatChecker())


def test_subkeys_vector_and_independent_hkdf():
    sub = V["subkeys"]
    assert (S.k_id(K).hex(), S.k_blob(K).hex(), S.k_meta(K).hex(), S.k_tag(K).hex()) == (sub["k_id"], sub["k_blob"], sub["k_meta"], sub["k_tag"])
    # RFC 5869 with an empty salt (= HashLen zero bytes), written out
    import hmac
    prk = hmac.new(b"\x00" * 32, K, hashlib.sha256).digest()
    for info, want in ((b"l0wagent-space/1 blob-id", sub["k_id"]), (b"l0wagent-space/1 lock-tag", sub["k_tag"])):
        assert hmac.new(prk, info + b"\x01", hashlib.sha256).digest().hex() == want
    assert len({sub["k_id"], sub["k_blob"], sub["k_meta"], sub["k_tag"]}) == 4
    with pytest.raises(ValidationError):
        S.k_id(b"short")


def test_blob_vector():
    b = V["blob"]
    contents = bytes.fromhex(b["contents_hex"])
    assert S.blob_id(K, V["epoch"], contents) == b["id"] and b["id"].startswith("e2.")
    bid, stored = S.blob_encrypt(K, SPACE, V["epoch"], contents)
    assert (bid, stored.hex()) == (b["id"], b["stored_hex"]) and stored[:1] == b"\x01"
    assert S.blob_decrypt(K, SPACE, bid, stored) == contents
    assert S.blob_id(K, 1, contents) != bid  # the epoch is part of the id


def test_blob_rejections():
    contents = b"data"
    bid, stored = S.blob_encrypt(K, SPACE, 1, contents)
    assert S.blob_encrypt(K, SPACE, 1, contents) == (bid, stored)  # same file, same epoch, same blob
    with pytest.raises(SealError):
        S.blob_decrypt(K, "other-space", bid, stored)
    with pytest.raises(SealError):
        S.blob_decrypt(bytes(32), SPACE, bid, stored)
    with pytest.raises(SealError):
        S.blob_decrypt(K, SPACE, bid, b"\x02" + stored[1:])
    with pytest.raises(SealError):
        S.blob_decrypt(K, SPACE, bid, stored[:-1] + bytes([stored[-1] ^ 1]))
    with pytest.raises(SealError):
        S.blob_decrypt(K, SPACE, bid, b"")
    # a blob stored under another blob's id (valid ciphertext of other contents under that key) is refused
    other_id, other_stored = S.blob_encrypt(K, SPACE, 1, b"different")
    with pytest.raises(SealError):
        S.blob_decrypt(K, SPACE, other_id, stored)
    with pytest.raises(ValidationError):
        S.blob_decrypt(K, SPACE, "x1.abc", stored)


def test_blob_id_is_a_valid_id_and_empty_file_works():
    bid, stored = S.blob_encrypt(K, SPACE, 3, b"")
    assert S.parse_blob_id(bid)[0] == 3 and S.blob_decrypt(K, SPACE, bid, stored) == b""
    for bad in ("e0.x", "e1.short", "E1." + "A" * 43, "e1" + "A" * 43, 7):
        with pytest.raises(ValidationError):
            S.parse_blob_id(bad)


def test_meta_vector_and_rejections():
    m = V["meta"]
    manifest = json.loads(m["manifest_json"])
    sealed = S.meta_seal(K, SPACE, m["seq"], m["author"], manifest, nonce=bytes.fromhex(m["nonce_hex"]))
    assert sealed == m["sealed"]
    assert S.meta_open(K, SPACE, m["seq"], m["author"], sealed) == manifest
    for args in ((SPACE, m["seq"] + 1, m["author"]), (SPACE, m["seq"], "eve@hub.example"), ("other", m["seq"], m["author"])):
        with pytest.raises(SealError):
            S.meta_open(K, *args, sealed)
    with pytest.raises(SealError):
        S.meta_open(bytes(32), SPACE, m["seq"], m["author"], sealed)
    with pytest.raises(SealError):
        S.meta_open(K, SPACE, m["seq"], m["author"], {**sealed, "nonce": b64.encode(bytes(11))})
    with pytest.raises(SealError):
        S.meta_open(K, SPACE, m["seq"], m["author"], "nope")


def test_meta_random_nonce_differs():
    manifest = {"message": "x", "files": {}}
    a, b = (S.meta_seal(K, SPACE, 1, ADDR, manifest) for _ in range(2))
    assert a["nonce"] != b["nonce"] and len(b64.decode(a["nonce"])) == 12
    assert S.meta_open(K, SPACE, 1, ADDR, a) == manifest


def test_manifest_validation():
    bid, _ = S.blob_encrypt(K, SPACE, 1, b"x")
    good = {"message": "m", "files": {"a/b": {"blob": bid, "size": 1, "exec": True}}, "conflicts": ["a/b"]}
    assert S.validate_manifest(good) == good
    assert S.manifest_blobs(good) == [bid]
    for mut in ({"files": {"../x": good["files"]["a/b"]}}, {"files": {"a": {"blob": "nope", "size": 1, "exec": False}}},
                {"files": {"a": {"blob": bid, "size": -1, "exec": False}}}, {"files": {"a": {"blob": bid, "size": 1}}},
                {"files": []}, {"conflicts": ["/x"]}, {"message": 3}):
        with pytest.raises(ValidationError):
            S.validate_manifest({**good, **mut})
    S.check_manifest_blobs(good, [bid])
    with pytest.raises(ValidationError):
        S.check_manifest_blobs(good, [])


def test_path_rules():
    for p in V["paths"]["valid"]:
        assert S.valid_path(p), p
    for p in V["paths"]["invalid"]:
        assert not S.valid_path(p), p
    assert not S.valid_path(None) and not S.valid_path("\ud800")
    with pytest.raises(ValidationError):
        S.check_path("a//b")


def test_lock_vector_and_rejections():
    lk = V["lock"]
    tag = S.lock_tag(K, lk["path"])
    assert tag == lk["tag"] and len(b64.decode(tag)) == 16
    sealed = S.lock_seal(K, SPACE, tag, lk["path"], lk["reason"], nonce=bytes.fromhex(lk["nonce_hex"]))
    assert sealed == lk["sealed"]
    assert S.lock_open(K, SPACE, tag, sealed) == {"path": lk["path"], "reason": lk["reason"]}
    assert S.lock_tag(K, "other") != tag and S.lock_tag(bytes(32), lk["path"]) != tag
    with pytest.raises(SealError):
        S.lock_open(K, "other", tag, sealed)
    with pytest.raises(SealError):
        S.lock_open(K, SPACE, S.lock_tag(K, "x"), sealed)
    # sealed under the right tag but with a path that isn't the tag's file
    forged = S.lock_seal(K, SPACE, S.lock_tag(K, "x"), "y")
    with pytest.raises(ValidationError):
        S.lock_open(K, SPACE, S.lock_tag(K, "x"), forged)
    with pytest.raises(ValidationError):
        S.lock_tag(K, "../x")


def test_space_key_vector():
    sk = V["space_key"]
    doc = sk["document"]
    p = S.space_key_payload(doc, NOW)
    assert S.space_key_info_of(p).decode() == sk["info"]
    assert sk["info"] == "\n".join(["l0wagent-space-key/1", SPACE, "2", ADDR, p["member_kid"], V["creator"], p["granter_kid"]])
    member_seal = bytes.fromhex(sk["member_seal_key"]["seed"])
    assert S.open_space_key_with(member_seal, doc, NOW) == K
    # re-seal with the vector's ephemeral key reproduces the document's sealed part
    again = S.seal_space_key(K, keys.Key.from_dict(sk["member_seal_key"]["key"]), space=SPACE, epoch=2, member=ADDR,
                             member_kid=p["member_kid"], granter=V["creator"], granter_kid=p["granter_kid"],
                             eph_priv=bytes.fromhex(sk["ephemeral_seed"]))
    assert again == p["sealed"]
    creator = keys.Key.from_dict(V["creator_key"]["key"])
    S.verify_space_key(doc, creator, space=SPACE, member=ADDR, granter=V["creator"], epoch=2, now=NOW)
    with pytest.raises(SealError):
        S.open_space_key_with(bytes(32), doc, NOW)


def test_space_key_binds_its_fields():
    sk = V["space_key"]
    p = S.space_key_payload(sk["document"], NOW)
    priv = bytes.fromhex(sk["member_seal_key"]["seed"])
    for name, val in (("space", "other"), ("epoch", 3), ("member", "eve@hub.example"), ("granter", "eve@hub.example"),
                      ("member_kid", "k1." + "A" * 22), ("granter_kid", "k1." + "A" * 22)):
        with pytest.raises(SealError):
            S.open_space_key_payload(priv, {**p, name: val})
    creator = keys.Key.from_dict(V["creator_key"]["key"])
    for kw in ({"space": "other"}, {"member": "eve@hub.example"}, {"granter": "eve@hub.example"}, {"epoch": 9}):
        with pytest.raises(SignatureError):
            S.verify_space_key(sk["document"], creator, now=NOW, **kw)
    other = keys.from_seed(hashlib.sha256(b"x").digest(), "sign", IAT)
    with pytest.raises(SignatureError):
        S.verify_space_key(sk["document"], other, now=NOW)
    with pytest.raises(ValidationError):
        S.space_key_payload(V["space_version"]["document"], NOW)
    with pytest.raises(ValidationError):
        S.validate_space_key({**p, "sealed": {"alg": "x", "enc": "a", "ct": "b"}})


def test_space_version_vector():
    sv = V["space_version"]
    doc = sv["document"]
    author = keys.Key.from_dict(sv["verify_keys"][0])
    p = S.verify_space_version(doc, author, space=SPACE, author=ADDR, now=NOW)
    assert p["parent"] == sv["parent"] and p["seq"] == 7 and p["blobs"] == sorted(p["blobs"])
    sent = S.version_doc_bytes(doc)
    assert sent.decode() == sv["document_bytes"] and json.loads(sent) == doc
    vid = S.version_id(doc["payload"])
    assert vid == sv["version_id"] == S.version_id_of(doc) == S.version_id_of(json.loads(json.dumps(doc, indent=2)))
    assert vid == "v." + b64.encode(hashlib.sha256(doc["payload"].encode()).digest()[:16]) and S.valid_version_id(vid)
    manifest = S.meta_open(K, SPACE, p["seq"], p["author"], p["meta"])
    S.check_manifest_blobs(manifest, p["blobs"])
    assert S.manifest_blobs(manifest) == p["blobs"]
    S.check_chain(p, 6, sv["parent"])
    for head in ((5, sv["parent"]), (6, "v." + "A" * 22), (7, sv["parent"])):
        with pytest.raises(ValidationError):
            S.check_chain(p, *head)
    with pytest.raises(SignatureError):
        S.verify_space_version(doc, author, space="other", now=NOW)
    with pytest.raises(SignatureError):
        S.verify_space_version(doc, author, author="eve@hub.example", now=NOW)


def test_version_id_changes_with_any_byte():
    assert S.version_id(b"a") != S.version_id(b"b") and len(b64.decode(S.version_id(b"a")[2:])) == 16


def test_build_and_validate_space_version():
    bid, _ = S.blob_encrypt(K, SPACE, 1, b"x")
    meta = S.meta_seal(K, SPACE, 1, ADDR, {"files": {"a": {"blob": bid, "size": 1, "exec": False}}})
    p = S.build_space_version(SPACE, 1, None, 1, ADDR, [bid, bid], meta, iat=IAT)
    assert p["blobs"] == [bid] and p["parent"] is None
    S.validate_space_version(p)
    for mut in ({"parent": "v." + "A" * 22}, {"seq": 0}, {"epoch": 0}, {"author": "nope"}, {"blobs": [bid, bid]}, {"blobs": ["x"]},
                {"meta": {"nonce": "AAAA", "ct": "AA"}}, {"meta": {"ct": "AA"}}, {"type": "space-key"}):
        with pytest.raises(ValidationError):
            S.validate_space_version({**p, **mut})
    with pytest.raises(ValidationError):
        S.validate_space_version({**p, "seq": 2})  # the second version needs a parent
    S.validate_space_version({**p, "seq": 2, "parent": "v." + "A" * 22})


def test_roundtrip_with_fresh_keys():
    member_seal = keys.generate("seal")
    creator = keys.generate("sign")
    k = b"\x07" * 32
    sealed = S.seal_space_key(k, member_seal.key.pub, space="s", epoch=1, member=ADDR, member_kid=member_seal.kid,
                              granter="bob@hub.example", granter_kid=creator.kid)
    p = S.build_space_key("s", 1, ADDR, member_seal.kid, "bob@hub.example", creator.kid, sealed, iat=IAT)
    doc = docs.sign_document("space-key", p, [creator])
    assert S.open_space_key_with(member_seal.private, doc, NOW) == k
    assert S.verify_space_key(doc, creator.key, now=NOW) == p


# --- schemas -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name,section", [("space-key", "space_key"), ("space-version", "space_version")])
def test_schema_validates_vector(name, section):
    v = V[section]
    schema("signed-document").validate(v["document"])
    pv = schema(name)
    payload = json.loads(v["payload_json"])
    pv.validate(payload)
    pv.validate({**payload, "future": {"x": [1]}})
    for f in ("v", "type", "iat") + tuple(k for k in payload if k not in ("v", "type", "iat", "exp")):
        assert not pv.is_valid({k: x for k, x in payload.items() if k != f}), f


def test_schema_rejects_bad_shapes():
    kv = schema("space-key")
    kp_ = json.loads(V["space_key"]["payload_json"])
    for mut in ({"epoch": 0}, {"epoch": "2"}, {"member": "Mara@Hub"}, {"granter_kid": "k2.x"}, {"sealed": {"alg": "x", "enc": "a", "ct": "b"}},
                {"space": ""}):
        assert not kv.is_valid({**kp_, **mut}), mut
    vv = schema("space-version")
    vp = json.loads(V["space_version"]["payload_json"])
    for mut in ({"seq": 0}, {"seq": 1}, {"parent": None}, {"parent": "x"}, {"epoch": 0}, {"blobs": ["x"]},
                {"blobs": [vp["blobs"][0], vp["blobs"][0]]}, {"meta": {"nonce": "AAAA", "ct": "AA"}}, {"author": "x"}):
        assert not vv.is_valid({**vp, **mut}), mut
    assert vv.is_valid({**vp, "seq": 1, "parent": None})
    assert not vv.is_valid({**copy.deepcopy(vp), "seq": 2, "parent": None})
