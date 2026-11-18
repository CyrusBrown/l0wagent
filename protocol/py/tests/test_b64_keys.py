import pytest

from l0wproto import b64, keys
from l0wproto.errors import EncodingError, KeyFormatError


@pytest.mark.parametrize("data", [b"", b"\x00", b"\xfb\xff", b"abc", bytes(range(64))])
def test_roundtrip(data):
    s = b64.encode(data)
    assert "=" not in s and "+" not in s and "/" not in s
    assert b64.decode(s) == data


@pytest.mark.parametrize("bad", ["AA==", "AA=", "+/8", "AB", "A", "AA AA", "AA\n", "é", "AAAA\n"])
def test_strict_decode_rejects(bad):
    with pytest.raises(EncodingError):
        b64.decode(bad)


def test_decode_non_string():
    with pytest.raises(EncodingError):
        b64.decode(b"AAAA")  # type: ignore[arg-type]


def test_kid_format_and_verify(kp):
    k = kp["sign"].key
    assert k.kid == keys.compute_kid("Ed25519", k.pub)
    assert k.kid.startswith("k1.") and len(k.kid) == 3 + 22
    assert keys.verify_kid(k)
    assert keys.Key.from_dict(k.to_dict()) == k


def test_kid_depends_on_alg(kp):
    assert keys.compute_kid("Ed25519", kp["sign"].key.pub) != keys.compute_kid("X25519", kp["sign"].key.pub)


def test_bad_kid_rejected(kp):
    d = kp["sign"].key.to_dict()
    d["kid"] = kp["seal"].key.kid
    with pytest.raises(KeyFormatError):
        keys.Key.from_dict(d)
    d = kp["sign"].key.to_dict()
    d["pub"] = kp["other"].key.to_dict()["pub"]
    with pytest.raises(KeyFormatError):
        keys.Key.from_dict(d)


def test_padded_pub_rejected(kp):
    d = kp["sign"].key.to_dict()
    d["pub"] += "="
    with pytest.raises(EncodingError):
        keys.Key.from_dict(d)


def test_unknown_alg_is_unusable_not_error():
    pub = b"\x01" * 40
    d = {"kid": keys.compute_kid("Falcon", pub), "alg": "Falcon", "use": "sign", "pub": b64.encode(pub), "created": "2026-11-17T21:30:00Z"}
    k = keys.Key.from_dict(d)
    assert not k.usable and not k.verify(b"x" * 64, b"m")


def test_generate_and_x25519():
    g = keys.generate("seal")
    assert g.key.alg == "X25519" and g.key.use == "seal"
    with pytest.raises(KeyFormatError):
        g.sign(b"x")
    r = keys.generate("recover")
    assert r.key.alg == "Ed25519" and r.key.use == "recover"


def test_signature_roundtrip(kp):
    s = kp["sign"].sign(b"hello")
    assert kp["sign"].key.verify(s, b"hello") and not kp["sign"].key.verify(s, b"hellO")
    assert not kp["other"].key.verify(s, b"hello")
