import pytest

from conftest import ADDR, NOW
from l0wproto import hopsig as H
from l0wproto.errors import HopAuthError

URI = "https://hub.example/v0/members/mara/leave"


def setup(kp, body=b'{"a":1}', ctype="application/json", **kw):
    sr = H.sign_request("POST" if body else "GET", URI, body, ctype if body else None, kp["sign"], ADDR, created=NOW,
                        nonce="bm9uY2U", **kw)
    resolver = lambda addr, kid: kp["sign"].key if (addr, kid) == (ADDR, kp["sign"].kid) else None  # noqa: E731
    return sr, resolver


def headers(sr, ctype="application/json"):
    h = dict(sr.headers)
    if "Content-Digest" in h:
        h["Content-Type"] = ctype
    return h


def verify(sr, resolver, body=b'{"a":1}', method="POST", uri=URI, now=NOW, cache=None, h=None):
    return H.verify_request(method, uri, h if h is not None else headers(sr), body, resolver, cache or H.InMemoryNonceCache(lambda: now), now)


def test_roundtrip_body(kp):
    sr, r = setup(kp)
    res = verify(sr, r)
    assert res.address == ADDR and res.keyid == kp["sign"].kid
    assert sr.headers["Signature-Input"].startswith('l0w=("@method" "@target-uri" "content-digest" "content-type");created=')
    assert sr.signature_base.splitlines()[-1].startswith('"@signature-params": ("@method"')
    assert sr.headers["Content-Digest"] == H.content_digest(b'{"a":1}')
    assert sr.headers["Signature"].startswith("l0w=:") and sr.headers["Signature"].endswith(":")


def test_roundtrip_no_body(kp):
    sr, r = setup(kp, body=b"")
    assert 'content-digest' not in sr.headers["Signature-Input"]
    verify(sr, r, body=b"", method="GET")


def test_params_order_and_values(kp):
    sr, _ = setup(kp)
    assert sr.params.endswith(f';created={NOW};expires={NOW + 300};nonce="bm9uY2U";keyid="{kp["sign"].kid}";alg="ed25519";tag="l0w/0.1"')


def test_content_digest_value():
    # RFC 9530 example body {"hello": "world"}
    assert H.content_digest(b'{"hello": "world"}') == "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"


def test_tampered(kp):
    sr, r = setup(kp)
    for kwargs in (dict(body=b'{"a":2}'), dict(method="PUT"), dict(uri=URI + "x")):
        with pytest.raises(HopAuthError):
            verify(sr, r, **kwargs)
    h = headers(sr)
    h["Content-Type"] = "text/plain"
    with pytest.raises(HopAuthError):
        verify(sr, r, h=h)
    h = headers(sr)
    h["L0W-Signer"] = "bob@hub.example"
    with pytest.raises(HopAuthError):
        verify(sr, r, h=h)
    h = headers(sr)
    h["Signature"] = H.sign_request("POST", URI, b"zz", "application/json", kp["other"], ADDR, created=NOW).headers["Signature"]
    with pytest.raises(HopAuthError):
        verify(sr, r, h=h)


def test_header_names_case_insensitive(kp):
    sr, r = setup(kp)
    verify(sr, r, h={k.lower(): v for k, v in headers(sr).items()})


def test_missing_parts(kp):
    sr, r = setup(kp)
    for drop in ("Signature", "Signature-Input", "Content-Digest", "L0W-Signer"):
        h = headers(sr)
        del h[drop]
        with pytest.raises(HopAuthError):
            verify(sr, r, h=h)
    h = headers(sr)
    del h["Content-Type"]
    with pytest.raises(HopAuthError):
        verify(sr, r, h=h)


def test_body_present_but_not_covered(kp):
    sr, r = setup(kp, body=b"")
    with pytest.raises(HopAuthError):
        verify(sr, r, body=b"x", method="GET", h=dict(sr.headers))


def test_time_window(kp):
    sr, r = setup(kp)
    verify(sr, r, now=NOW + 300)
    with pytest.raises(HopAuthError):
        verify(sr, r, now=NOW + 301)  # expired
    verify(sr, r, now=NOW - 300)
    with pytest.raises(HopAuthError):
        verify(sr, r, now=NOW - 301)  # created in the future


def test_ttl_limit(kp):
    with pytest.raises(Exception):
        setup(kp, ttl=301)
    sr, r = setup(kp)
    h = headers(sr)
    h["Signature-Input"] = h["Signature-Input"].replace(f"expires={NOW + 300}", f"expires={NOW + 301}")
    with pytest.raises(HopAuthError):
        verify(sr, r, h=h)


def test_tag_alg(kp):
    sr, r = setup(kp)
    for old, new in (('tag="l0w/0.1"', 'tag="l0w/9.9"'), ('alg="ed25519"', 'alg="rsa-v1_5-sha256"')):
        h = headers(sr)
        h["Signature-Input"] = h["Signature-Input"].replace(old, new)
        with pytest.raises(HopAuthError):
            verify(sr, r, h=h)


def test_unknown_key_and_wrong_use(kp):
    sr, _ = setup(kp)
    with pytest.raises(HopAuthError):
        verify(sr, lambda a, k: None)
    with pytest.raises(HopAuthError):
        verify(sr, lambda a, k: kp["recover"].key)  # kid != keyid
    sr2 = H.sign_request("POST", URI, b'{"a":1}', "application/json", kp["recover"], ADDR, created=NOW)
    with pytest.raises(HopAuthError):  # recovery keys don't sign requests
        verify(sr2, lambda a, k: kp["recover"].key)


def test_replay(kp):
    sr, r = setup(kp)
    cache = H.InMemoryNonceCache(lambda: NOW)
    verify(sr, r, cache=cache)
    with pytest.raises(HopAuthError):
        verify(sr, r, cache=cache)
    # a different nonce is fine
    sr2 = H.sign_request("POST", URI, b'{"a":1}', "application/json", kp["sign"], ADDR, created=NOW, nonce="b3RoZXI")
    verify(sr2, r, cache=cache)


def test_failed_verification_does_not_burn_nonce(kp):
    sr, r = setup(kp)
    cache = H.InMemoryNonceCache(lambda: NOW)
    with pytest.raises(HopAuthError):
        verify(sr, r, body=b"bad", cache=cache)
    verify(sr, r, cache=cache)


def test_nonce_cache_expiry():
    t = [100]
    c = H.InMemoryNonceCache(lambda: t[0])
    assert c.add_if_new("k", "n", 200) and not c.add_if_new("k", "n", 200)
    assert c.add_if_new("k2", "n", 200)
    t[0] = 201
    assert c.add_if_new("k", "n", 500)
    with pytest.raises(NotImplementedError):
        H.NonceCache().add_if_new("k", "n", 1)


def test_error_has_no_detail(kp):
    with pytest.raises(HopAuthError) as e:
        verify(*setup(kp), body=b"x")
    assert e.value.body()["error"] == "bad_signature" and e.value.body()["detail"] == "" and e.value.reason


def test_garbage_headers(kp):
    sr, r = setup(kp)
    for si in ("", "garbage", 'l0w=("@method");created=x', 'other=("@method")'):
        h = headers(sr)
        h["Signature-Input"] = si
        with pytest.raises(HopAuthError):
            verify(sr, r, h=h)
    for s in ("", "l0w=:!!!:", "l0w=:AAAA:"):
        h = headers(sr)
        h["Signature"] = s
        with pytest.raises(HopAuthError):
            verify(sr, r, h=h)
