import copy

import pytest

from conftest import ADDR, HUB, IAT, NOW
from l0wproto import b64, docs, payloads
from l0wproto.errors import DocumentError, SignatureError, UnsupportedVersion, ValidationError
from l0wproto.times import format_time


def mk(kp, type_="moved", signers=("sign",), **over):
    p = payloads.build_moved(ADDR, "mara@other.example", iat=IAT)
    p.update(over)
    p["type"] = over.get("type", type_)
    return docs.sign_document(type_ if "type" not in over else over["type"], p, [kp[s] for s in signers])


def test_signing_input_exact(kp):
    d = mk(kp)
    assert docs.signing_input("moved", d["payload"]) == f"l0wagent-sig/1\nmoved\n{d['payload']}".encode()


def test_roundtrip_and_payload_exact_bytes(kp):
    d = mk(kp)
    p = docs.verify_document(d, [kp["sign"].key], expected_type="moved", now=NOW)
    assert p["from"] == ADDR
    assert b64.decode(d["payload"]) == docs.serialize_payload(p)


def test_flipped_payload_byte(kp):
    d = mk(kp)
    raw = bytearray(b64.decode(d["payload"]))
    raw[-3] ^= 1
    d["payload"] = b64.encode(bytes(raw))
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_flipped_payload_char(kp):
    d = mk(kp)
    c = d["payload"]
    d["payload"] = c[:10] + ("A" if c[10] != "A" else "B") + c[11:]
    with pytest.raises((SignatureError, DocumentError)):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_wrong_outer_type(kp):
    d = mk(kp)
    d["type"] = "leave"
    with pytest.raises(SignatureError):  # signature was over type "moved"
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_expected_type(kp):
    with pytest.raises(DocumentError):
        docs.verify_document(mk(kp), [kp["sign"].key], expected_type="leave", now=NOW)


def test_inner_outer_type_mismatch_even_when_signed(kp):
    # signer signs outer 'leave' over a payload whose inner type is 'moved'
    p = payloads.build_moved(ADDR, "mara@other.example", iat=IAT)
    b = b64.encode(docs.serialize_payload(p))
    sig = kp["sign"].sign(docs.signing_input("leave", b))
    d = {"v": "l0w/0.1", "type": "leave", "payload": b, "sigs": [{"kid": kp["sign"].kid, "sig": b64.encode(sig)}]}
    with pytest.raises(ValidationError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)
    with pytest.raises(DocumentError):
        docs.sign_document("leave", p, [kp["sign"]])


def test_swapped_signature(kp):
    a, b = mk(kp), mk(kp, to="mara@third.example")
    a["sigs"] = b["sigs"]
    with pytest.raises(SignatureError):
        docs.verify_document(a, [kp["sign"].key], now=NOW)


def test_signature_from_other_key(kp):
    d = mk(kp, signers=("other",))
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_unknown_kid_rejected_even_with_valid_sig(kp):
    d = mk(kp, signers=("sign", "other"))
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)
    docs.verify_document(d, [kp["sign"].key, kp["other"].key], now=NOW)


def test_bad_kid_string(kp):
    d = mk(kp)
    d["sigs"][0]["kid"] = "k1.AAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_padded_signature_and_payload(kp):
    d = mk(kp)
    d["sigs"][0]["sig"] += "="
    with pytest.raises(Exception) as e:
        docs.verify_document(d, [kp["sign"].key], now=NOW)
    assert e.value.code in ("bad_encoding", "bad_signature")
    d = mk(kp)
    d["payload"] += "="
    with pytest.raises(Exception):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_required_each_signer(kp):
    d = mk(kp, signers=("sign",))
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key, kp["recover"].key], required=[kp["sign"].kid, kp["recover"].kid], now=NOW)
    d2 = mk(kp, signers=("sign", "recover"))
    docs.verify_document(d2, [kp["sign"].key, kp["recover"].key], required=[kp["sign"].kid, kp["recover"].kid], now=NOW)


def test_no_sigs(kp):
    d = mk(kp)
    d["sigs"] = []
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_duplicate_signer(kp):
    d = mk(kp)
    d["sigs"] = d["sigs"] * 2
    with pytest.raises(SignatureError):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


def test_version(kp):
    d = mk(kp)
    d["v"] = "l0w/9.9"
    with pytest.raises(UnsupportedVersion) as e:
        docs.verify_document(d, [kp["sign"].key], now=NOW)
    assert e.value.code == "unsupported_version"


def test_iat_skew(kp):
    d = mk(kp)
    docs.verify_document(d, [kp["sign"].key], now=NOW - 300)
    with pytest.raises(ValidationError):
        docs.verify_document(d, [kp["sign"].key], now=NOW - 301)
    docs.verify_document(d, [kp["sign"].key], now=NOW + 10**6)  # old documents are fine


def test_exp(kp):
    p = payloads.build_moved(ADDR, "mara@other.example", iat=IAT, exp=NOW + 60)
    d = docs.sign_document("moved", p, [kp["sign"]])
    docs.verify_document(d, [kp["sign"].key], now=NOW + 60)
    with pytest.raises(ValidationError):
        docs.verify_document(d, [kp["sign"].key], now=NOW + 61)


def test_unknown_fields_ignored(kp):
    p = payloads.build_moved(ADDR, "mara@other.example", iat=IAT)
    p["future"] = {"x": 1}
    d = docs.sign_document("moved", p, [kp["sign"]])
    payloads.verify_moved(d, kp["sign"].key, now=NOW)


def test_sign_requires_fields(kp):
    with pytest.raises(DocumentError):
        docs.sign_document("moved", {"v": "l0w/0.1", "type": "moved"}, [kp["sign"]])


def test_malformed_documents(kp):
    for bad in (None, [], {}, {"v": "l0w/0.1"}, {"v": "l0w/0.1", "type": "x", "payload": 5}):
        with pytest.raises(DocumentError):
            docs.verify_document(bad, [kp["sign"].key])  # type: ignore[arg-type]
    d = mk(kp)
    d["payload"] = b64.encode(b"[1,2]")
    with pytest.raises((SignatureError, ValidationError)):
        docs.verify_document(d, [kp["sign"].key], now=NOW)


# --- directory entries ---


def make_self(kp, **over):
    p = payloads.build_self(ADDR, 1, [kp["sign"].key, kp["seal"].key, kp["recover"].key],
                            {"instance": "mara.hub.example", "files": "files--mara.hubfiles.example"}, iat=IAT)
    p.update(over)
    return docs.sign_document("self", p, [kp["sign"]])


def test_directory_roundtrip(kp):
    e = docs.countersign_directory(make_self(kp), kp["hub"])
    p = docs.verify_directory_entry(e, kp["hub"].key, hub_domain=HUB, name="mara", now=NOW)
    assert p["address"] == ADDR
    # the member's own signature is untouched and the hub signs a different input
    assert e["sigs"] == make_self(kp)["sigs"]
    assert docs.signing_input("directory-entry", e["payload"]) != docs.signing_input("self", e["payload"])


def test_directory_hub_sig_not_a_member_sig(kp):
    e = docs.countersign_directory(make_self(kp), kp["hub"])
    e2 = copy.deepcopy(e)
    e2["sigs"] = [{"kid": kp["hub"].kid, "sig": e["hub_sig"]["sig"]}]
    with pytest.raises(SignatureError):
        docs.verify_directory_entry(e2, kp["hub"].key, hub_domain=HUB, now=NOW)


def test_directory_failures(kp):
    e = docs.countersign_directory(make_self(kp), kp["hub"])
    for mutate, exc in (
        (lambda x: x.pop("hub_sig"), SignatureError),
        (lambda x: x["hub_sig"].update(sig=e["sigs"][0]["sig"]), SignatureError),
        (lambda x: x["hub_sig"].update(kid=kp["other"].kid), SignatureError),
    ):
        x = copy.deepcopy(e)
        mutate(x)
        with pytest.raises(exc):
            docs.verify_directory_entry(x, kp["hub"].key, hub_domain=HUB, now=NOW)
    with pytest.raises(ValidationError):
        docs.verify_directory_entry(e, kp["hub"].key, hub_domain="other.example", now=NOW)
    with pytest.raises(ValidationError):
        docs.verify_directory_entry(e, kp["hub"].key, hub_domain=HUB, name="bob", now=NOW)
    with pytest.raises(SignatureError):
        docs.verify_directory_entry(e, kp["other"].key, hub_domain=HUB, now=NOW)


def test_directory_tampered_payload(kp):
    e = docs.countersign_directory(make_self(kp), kp["hub"])
    other = make_self(kp, seq=2)
    e["payload"] = other["payload"]
    with pytest.raises(SignatureError):
        docs.verify_directory_entry(e, kp["hub"].key, hub_domain=HUB, now=NOW)


def test_countersign_only_self(kp):
    with pytest.raises(DocumentError):
        docs.countersign_directory(mk(kp), kp["hub"])


def test_times():
    assert format_time(NOW) == IAT
