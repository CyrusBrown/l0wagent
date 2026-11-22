"""HPKE (RFC 9180) base mode for envelope bodies (messages.md "Sealing").

Suite: DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20Poly1305, i.e. KEM 0x0020, KDF 0x0001, AEAD 0x0003.
Written out from X25519, HMAC and ChaCha20Poly1305 primitives rather than a library's HPKE so the
ephemeral key can be fixed for test vectors; it is checked against RFC 9180 A.2.1 and against
`cryptography`'s own HPKE in the tests. Everything private stays a raw 32-byte X25519 key.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import b64
from .errors import SealError

ALG = "HPKE-v1-X25519-SHA256-ChaCha20Poly1305"
KEM_ID, KDF_ID, AEAD_ID = 0x0020, 0x0001, 0x0003
_NH, _NK, _NN, _NPK = 32, 32, 12, 32
_SUITE_KEM = b"KEM" + KEM_ID.to_bytes(2, "big")
_SUITE = b"HPKE" + KEM_ID.to_bytes(2, "big") + KDF_ID.to_bytes(2, "big") + AEAD_ID.to_bytes(2, "big")
_RAW = serialization.Encoding.Raw
_RAWPUB = serialization.PublicFormat.Raw


def seal_info(from_addr: str, from_kid: str, to_addr: str, to_kid: str, env_id: str) -> bytes:
    """`"l0wagent-seal/1\\n" + from + "\\n" + from_kid + "\\n" + to + "\\n" + to_kid + "\\n" + id`, ASCII."""
    try:
        return f"l0wagent-seal/1\n{from_addr}\n{from_kid}\n{to_addr}\n{to_kid}\n{env_id}".encode("ascii")
    except UnicodeEncodeError as e:
        raise SealError("info must be ASCII") from e


# --- RFC 9180 pieces -------------------------------------------------------------------------


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or b"\x00" * _NH, ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t = b"", b""
    for i in range(1, -(-length // _NH) + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]


def _lextract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite + label + ikm)


def _lexpand(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    return _expand(prk, length.to_bytes(2, "big") + b"HPKE-v1" + suite + label + info, length)


def derive_key_pair(ikm: bytes) -> tuple[bytes, bytes]:
    """RFC 9180 DeriveKeyPair for X25519 -> (private, public), both raw 32 bytes."""
    prk = _lextract(_SUITE_KEM, b"", b"dkp_prk", ikm)
    sk = _lexpand(_SUITE_KEM, prk, b"sk", b"", 32)
    return sk, _pub(sk)


def _pub(priv: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(_RAW, _RAWPUB)


def _dh(priv: bytes, pub: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError as e:  # bad length, or an all-zero (low-order) result
        raise SealError("key agreement failed") from e


def _shared_secret(dh: bytes, enc: bytes, pk_r: bytes) -> bytes:
    eae_prk = _lextract(_SUITE_KEM, b"", b"eae_prk", dh)
    return _lexpand(_SUITE_KEM, eae_prk, b"shared_secret", enc + pk_r, _NH)


def _key_schedule(shared: bytes, info: bytes) -> tuple[bytes, bytes]:
    """Base mode (0), no psk. Returns (key, base_nonce)."""
    psk_id_hash = _lextract(_SUITE, b"", b"psk_id_hash", b"")
    info_hash = _lextract(_SUITE, b"", b"info_hash", info)
    ctx = b"\x00" + psk_id_hash + info_hash
    secret = _lextract(_SUITE, shared, b"secret", b"")
    return _lexpand(_SUITE, secret, b"key", ctx, _NK), _lexpand(_SUITE, secret, b"base_nonce", ctx, _NN)


def hpke_seal(pk_r: bytes, info: bytes, aad: bytes, plaintext: bytes, *, eph_priv: bytes | None = None) -> tuple[bytes, bytes]:
    """Single-shot base-mode seal (sequence number 0) -> (enc, ct). `eph_priv` is for test vectors only."""
    if len(pk_r) != _NPK:
        raise SealError("recipient key must be 32 bytes")
    sk_e = eph_priv if eph_priv is not None else os.urandom(32)
    enc = _pub(sk_e)
    shared = _shared_secret(_dh(sk_e, pk_r), enc, pk_r)
    key, nonce = _key_schedule(shared, info)
    return enc, ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def hpke_open(sk_r: bytes, enc: bytes, info: bytes, aad: bytes, ct: bytes) -> bytes:
    if len(enc) != _NPK or len(sk_r) != 32:
        raise SealError("cannot open")
    pk_r = _pub(sk_r)
    shared = _shared_secret(_dh(sk_r, enc), enc, pk_r)
    key, nonce = _key_schedule(shared, info)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    except Exception as e:
        raise SealError("cannot open") from e


# --- the document form -----------------------------------------------------------------------


def seal(recipient_pub_raw: bytes, plaintext: bytes, info: bytes, *, eph_priv: bytes | None = None) -> dict:
    """-> {"alg", "enc", "ct"} (base64url). aad is empty."""
    enc, ct = hpke_seal(recipient_pub_raw, info, b"", plaintext, eph_priv=eph_priv)
    return {"alg": ALG, "enc": b64.encode(enc), "ct": b64.encode(ct)}


def open_sealed(recipient_priv: bytes, sealed: object, info: bytes) -> bytes:
    """Raises SealError on any failure (unknown alg, bad encoding, wrong key, wrong info, tampering)."""
    if not isinstance(sealed, dict) or sealed.get("alg") != ALG:
        raise SealError("cannot open")
    try:
        enc, ct = b64.decode(sealed.get("enc")), b64.decode(sealed.get("ct"))
    except Exception as e:
        raise SealError("cannot open") from e
    return hpke_open(recipient_priv, enc, info, b"", ct)
