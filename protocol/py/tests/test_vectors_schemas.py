"""Vectors regenerate identically; the library verifies every vector; schemas validate the payloads."""

import copy
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from l0wproto import b64, docs, hopsig, invite, keys, names, payloads as P, tunnel
from l0wproto.errors import FrameError, InviteError, NameRuleError, SignatureError
from l0wproto.times import parse_time

PROTO = Path(__file__).resolve().parents[2]
VEC = PROTO / "vectors"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mv = load_script("make_vectors")
ms = load_script("make_schemas")

DOC_VECTORS = ["rotation", "rotation-recovery", "self", "self-after-rotation", "revocation", "moved", "hub", "registration", "leave",
               "reregistration", "tunnel-hello"]


def vec(name):
    return json.loads((VEC / f"{name}.json").read_text())


def test_vectors_regenerate_identically():
    for name, content in mv.build().items():
        assert (VEC / name).read_text() == content, name
    assert mv.build() == mv.build()
    assert set(mv.build()) == {p.name for p in VEC.glob("*.json")}


def test_schemas_and_openapi_regenerate_identically():
    for name, content in ms.all_files().items():
        assert (PROTO / name).read_text() == content, name


def test_seeds_in_vectors_reproduce_keys():
    for label, k in vec("keys")["keys"].items():
        kp = keys.from_seed(bytes.fromhex(k["seed"]), k["key"]["use"], k["key"]["created"])
        assert kp.key.to_dict() == k["key"], label


@pytest.mark.parametrize("name", DOC_VECTORS)
def test_document_vector(name):
    v = vec(name)
    now = parse_time(v["now"])
    doc = v["document"]
    assert doc["payload"] == v["payload_b64"] == b64.encode(v["payload_json"].encode())
    assert v["signing_input"] == f"l0wagent-sig/1\n{v['type']}\n{v['payload_b64']}"
    assert doc["sigs"] == v["signatures"]
    ks = [keys.Key.from_dict(k) for k in v["verify_keys"]]
    payload = docs.verify_document(doc, ks, expected_type=v["type"], required=v["required"], now=now)
    assert json.loads(v["payload_json"]) == payload
    # the typed verifier agrees
    typed = {
        "self": lambda: P.verify_self(doc, now), "self-after-rotation": lambda: P.verify_self(doc, now),
        "hub": lambda: P.verify_hub(doc, ks[0], now), "registration": lambda: P.verify_registration(doc, "hub.example", now),
        "moved": lambda: P.verify_moved(doc, ks[0], now), "leave": lambda: P.verify_leave(doc, ks[0], now),
        "reregistration": lambda: P.verify_reregistration(doc, ks[0], now),
        "revocation": lambda: P.verify_revocation(doc, ks, now),
        "rotation": lambda: P.verify_rotation(doc, keys.Key.from_dict(vec("keys")["keys"]["member-sign"]["key"]),
                                              keys.Key.from_dict(vec("keys")["keys"]["member-recover"]["key"]), now),
        "rotation-recovery": lambda: P.verify_rotation(doc, keys.Key.from_dict(vec("keys")["keys"]["member-sign"]["key"]),
                                                       keys.Key.from_dict(vec("keys")["keys"]["member-recover"]["key"]), now),
        "tunnel-hello": lambda: P.verify_tunnel_hello(doc, ks[0], nonce=v["challenge"]["nonce"], hub="hub.example", now=now),
    }[name]()
    assert typed == payload
    # tampering with the vector fails
    bad = copy.deepcopy(doc)
    raw = bytearray(b64.decode(bad["payload"]))
    raw[10] ^= 1
    bad["payload"] = b64.encode(bytes(raw))
    with pytest.raises(Exception):
        docs.verify_document(bad, ks, now=now)


def test_self_after_rotation_history_verifies():
    v = vec("self-after-rotation")
    rot = vec("rotation")["document"]
    assert json.loads(b64.decode(v["document"]["payload"]))["history"] == [rot]


def test_directory_entry_vector():
    v = vec("directory-entry")
    hub = keys.Key.from_dict(v["hub_key"])
    now = parse_time(v["now"])
    p = docs.verify_directory_entry(v["entry"], hub, hub_domain=v["hub_domain"], name=v["name"], now=now)
    assert p == json.loads(vec("self")["payload_json"])
    assert v["hub_signing_input"] == f"l0wagent-sig/1\ndirectory-entry\n{v['entry']['payload']}"
    assert v["entry"]["sigs"] == vec("self")["signatures"] and v["entry"]["hub_sig"] == v["hub_sig"]
    assert hub.verify(b64.decode(v["hub_sig"]["sig"]), v["hub_signing_input"].encode())


def test_basic_vectors():
    b = vec("base64url")
    for c in b["valid"]:
        assert b64.encode(bytes.fromhex(c["bytes_hex"])) == c["b64"] and b64.decode(c["b64"]) == bytes.fromhex(c["bytes_hex"])
    for c in b["invalid"]:
        with pytest.raises(Exception):
            b64.decode(c["b64"])
    n = vec("names")
    for x in n["valid"]:
        names.validate_name(x)
    for x in n["invalid"]:
        with pytest.raises(NameRuleError):
            names.validate_name(x)
    for x in n["reserved"]:
        with pytest.raises(NameRuleError):
            names.check_name(x)
    for c in n["lookalike"]:
        if c["existing_or_reserved"]:
            with pytest.raises(NameRuleError):
                names.check_name(c["candidate"], taken=[c["existing_or_reserved"]], reserved_extra=[])
        else:
            names.check_name(c["candidate"], taken=["mara"])
    i = vec("invites")
    for c in i["valid"]:
        p = invite.parse_invite(c["string"])
        assert (p.base_url, p.code) == (c["base_url"], c["code"])
    for s in i["invalid"]:
        with pytest.raises(InviteError):
            invite.parse_invite(s)


def test_hop_auth_vector():
    v = vec("hop-auth")
    key = keys.Key.from_dict(v["key"]["key"])
    kp = keys.from_seed(bytes.fromhex(v["key"]["seed"]), "sign", key.created)
    assert kp.key == key
    now = parse_time(v["now"])
    for c in v["cases"]:
        r = c["request"]
        body = r["body"].encode()
        sr = hopsig.sign_request(r["method"], r["target_uri"], body, r["content_type"], kp, v["signer"], created=v["created"], nonce=v["nonce"])
        assert sr.signature_base == c["signature_base"] and sr.params == c["signature_params"] and sr.headers == c["headers"]
        h = dict(c["headers"])
        if r["content_type"]:
            h["Content-Type"] = r["content_type"]
        res = hopsig.verify_request(r["method"], r["target_uri"], h, body, lambda a, k: key, hopsig.InMemoryNonceCache(lambda: now), now)
        assert res.address == v["signer"]
        # independently re-derive the signature over the published base
        sig = c["headers"]["Signature"][len("l0w=:"):-1]
        import base64
        assert key.verify(base64.b64decode(sig), c["signature_base"].encode())


def test_frame_vectors():
    v = vec("tunnel-frames")
    for c in v["valid"]:
        if c.get("elided"):
            continue
        raw = bytes.fromhex(c["frame_hex"])
        f = tunnel.decode(raw)
        assert (f.type, f.flags, f.stream_id, f.payload.hex()) == (c["type"], c["flags"], c["stream_id"], c["payload_hex"])
        assert tunnel.encode(f) == raw
    for c in v["malformed"]:
        if c.get("frame_hex_elided"):
            continue
        with pytest.raises(FrameError):
            tunnel.decode(bytes.fromhex(c["frame_hex"]))


# --- schemas ---------------------------------------------------------------------------------

SCHEMA_FILES = sorted((PROTO / "schemas").glob("*.json"))


def registry():
    reg = Registry()
    for p in SCHEMA_FILES:
        s = json.loads(p.read_text())
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))
    return reg


def validator(name):
    s = json.loads((PROTO / "schemas" / f"{name}.json").read_text())
    return jsonschema.Draft202012Validator(s, registry=registry(), format_checker=jsonschema.FormatChecker())


def test_schemas_are_valid():
    assert len(SCHEMA_FILES) >= 17
    for p in SCHEMA_FILES:
        s = json.loads(p.read_text())
        jsonschema.Draft202012Validator.check_schema(s)
        assert s["$schema"].endswith("2020-12/schema") and s["$id"].endswith(p.name)


@pytest.mark.parametrize("name", DOC_VECTORS)
def test_schema_validates_vector(name):
    v = vec(name)
    typ = v["type"]
    validator("signed-document").validate(v["document"])
    pv = validator(typ)
    payload = json.loads(v["payload_json"])
    pv.validate(payload)
    # unknown extra fields are allowed
    pv.validate({**payload, "future": {"x": [1]}})
    # missing required fields are not
    for f in ("v", "type", "iat"):
        bad = {k: x for k, x in payload.items() if k != f}
        assert not pv.is_valid(bad), f
    for k in v["verify_keys"]:
        validator("key").validate(k)


def test_schema_rejects_bad_shapes():
    pv = validator("self")
    p = json.loads(vec("self")["payload_json"])
    assert pv.is_valid(p)
    for mut in ({"seq": "3"}, {"keys": p["keys"][:2]}, {"address": "Mara@Hub"}, {"iat": "2026-11-17 21:30:00"}, {"v": "l0w/9"},
                {"hostnames": {"instance": "x"}}):
        assert not pv.is_valid({**p, **mut}), mut
    sd = validator("signed-document")
    d = vec("self")["document"]
    assert not sd.is_valid({**d, "payload": d["payload"] + "="})
    assert not sd.is_valid({**d, "sigs": []})
    assert not validator("key").is_valid({**vec("keys")["keys"]["hub-sign"]["key"], "kid": "k2.x"})


def test_schema_directory_entry_join_error_invite():
    validator("directory-entry").validate(vec("directory-entry")["entry"])
    assert not validator("directory-entry").is_valid(vec("self")["document"])
    hubkey = vec("keys")["keys"]["hub-sign"]["key"]
    validator("join-response").validate(P.build_join_response("mara@hub.example", {"instance": "a", "files": "b"}, keys.Key.from_dict(hubkey), None))
    validator("join-response").validate(P.build_join_response("mara@hub.example", {"instance": "a", "files": "b"}, keys.Key.from_dict(hubkey), "wss://t"))
    validator("invite-response").validate(P.build_invite_response("2026-11-24T21:30:00Z", "Example hub"))
    validator("invite-response").validate(P.build_invite_response("2026-11-24T21:30:00Z", "Example hub", "mara"))
    e = validator("error")
    for code in ("no_such_invite", "bad_registration", "name_taken"):
        e.validate(SignatureError("x", code=code).body())
    e.validate({"error": "no_such_member"})
    e.validate({"v": "l0w/0.1", "error": "unsupported_version", "detail": "", "supported": ["l0w/0.1"]})
    assert not e.is_valid({"detail": "x"})
    c = tunnel.parse_text_message(tunnel.challenge_message(bytes(32), "hub.example"))
    validator("tunnel-challenge").validate(c)
    validator("tunnel-ready").validate(tunnel.parse_text_message(tunnel.ready_message("s", {})))


def test_openapi_structure_and_refs():
    api = json.loads((PROTO / "openapi.json").read_text())
    assert api["openapi"].startswith("3.1")
    paths = api["paths"]
    for p in ("/.well-known/l0wagent/hub", "/.well-known/l0wagent/members/{name}", "/.well-known/l0wagent/members/{name}/revocations",
              "/.well-known/l0wagent/members/{name}/reregistrations", "/invites/{code}", "/members", "/members/{name}/self",
              "/members/{name}/leave"):
        assert p in paths, p
    codes = json.dumps(api)
    for c in ("no_such_member", "left", "no_such_invite", "invite_used", "invite_expired", "bad_registration", "needs_invite",
              "name_taken", "name_reserved", "own_url_unreachable", "instance_too_old", "unsupported_version"):
        assert c in codes, c

    def refs(o):
        if isinstance(o, dict):
            if "$ref" in o:
                yield o["$ref"]
            for x in o.values():
                yield from refs(x)
        elif isinstance(o, list):
            for x in o:
                yield from refs(x)

    found = set(refs(api))
    assert found
    for r in found:
        assert (PROTO / r).is_file(), r
