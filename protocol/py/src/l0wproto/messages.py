"""Envelopes, delivery tokens, fingerprints and friend codes (messages.md)."""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import b64, payloads as P
from .consts import ENVELOPE_MAX_AGE, MAX_ENVELOPE, MAX_SKEW, VERSION
from .docs import _shape, serialize_payload, sign_document, verify_document
from .errors import DocumentError, ProtocolError, SealError, SignatureError, ValidationError
from .keys import Key, KeyPair
from .seal import open_sealed, seal, seal_info
from .times import now as _now, parse_time


def make_envelope(sender_sign_key_priv: KeyPair, sender_kid: str, from_addr: str, to_addr: str, to_seal_key: Key, kind: str,
                  body: dict, seq: int, token_doc: dict | None, *, env_id: str | None = None, iat: str | int | None = None,
                  eph_priv: bytes | None = None) -> dict:
    """Validate `body` for `kind`, seal it to `to_seal_key`, and sign the envelope. `token_doc` must be None
    for a friend request and a delivery-token document otherwise. `eph_priv` fixes the HPKE ephemeral key (vectors only)."""
    if sender_sign_key_priv.kid != sender_kid:
        raise ValidationError("sender_kid is not the signing key's kid")
    if to_seal_key.alg != "X25519" or to_seal_key.use != "seal":
        raise ValidationError("recipient key is not a seal key")
    P.validate_body(kind, body)
    env_id = env_id or P.new_id()
    info = seal_info(from_addr, sender_kid, to_addr, to_seal_key.kid, env_id)
    sealed = seal(to_seal_key.pub, serialize_payload(body), info, eph_priv=eph_priv)
    payload = P.build_envelope(from_addr, sender_kid, to_addr, to_seal_key.kid, env_id, seq, kind, sealed, token_doc, iat=iat)
    P.validate_envelope(payload)
    doc = sign_document("envelope", payload, [sender_sign_key_priv])
    if len(json.dumps(doc, separators=(",", ":")).encode("utf-8")) > MAX_ENVELOPE:
        raise ValidationError("envelope is over 8 MiB", "too_large")
    return doc


def envelope_payload(doc: dict) -> dict:
    """Step 2 of receiving: decode and validate the outer document and payload WITHOUT any signature
    or time check (a gateway reads `to`, `from`, the token and `kind` from this before step 4)."""
    _shape(doc)
    if doc["type"] != "envelope":
        raise DocumentError("not an envelope")
    try:
        p = json.loads(b64.decode(doc["payload"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationError(f"payload is not valid JSON: {e}") from e
    if isinstance(p, dict) and (p.get("v"), p.get("type")) != (doc["v"], doc["type"]):
        raise ValidationError("inner v/type differ from outer")
    return P.validate_envelope(p)


def token_id(doc: dict) -> str | None:
    """The `id` of a delivery-token document, or of the token inside an envelope document (None when it
    carries none). Shape-checked, not signature-checked: a gateway uses it to look the token up in its records."""
    if isinstance(doc, dict) and doc.get("type") == "envelope":
        tok = envelope_payload(doc).get("token")
        return None if tok is None else token_id(tok)
    return P.validate_token_document(doc)["id"]


def verify_delivery_token(doc: dict, issuer_key: Key, holder_kid: str | None = None, *, now: int | None = None) -> dict:
    """The issuer's signature verifies, and the payload's `issuer_kid` is `issuer_key`'s kid. When `holder_kid`
    is given it must equal the token's `holder_kid` (to follow a holder's rotation, pass the kid the token
    was issued to). Whether the token is one the issuer still remembers is the caller's."""
    p = P.validate_delivery_token(
        verify_document(doc, [issuer_key], expected_type="delivery-token", required=[issuer_key.kid], now=now))
    if p["issuer_kid"] != issuer_key.kid:
        raise SignatureError("token was issued by another key")
    if holder_kid is not None and p["holder_kid"] != holder_kid:
        raise SignatureError("token was issued to another key")
    return p


def open_envelope(doc: dict, *, my_address: str, my_seal_keys: dict[str, bytes], sender_sign_key: Key,
                  now: int | None = None) -> tuple[dict, dict]:
    """Receiving steps 2, 4, 5 (time window) and 6 -> (payload, body). Token lookup (3), duplicate ids (5)
    and size/rate (1) are the caller's. `sender_sign_key` is the key `from_kid` resolves to (it must carry that kid).
    Errors carry the protocol code: not_here, bad_signature, bad_document, unknown_kind, bad_seal, bad_body."""
    n = _now() if now is None else now
    p = envelope_payload(doc)
    if P.split_address(p["to"]) != P.split_address(my_address):
        raise ProtocolError("not this instance's address", "not_here")
    if sender_sign_key.kid != p["from_kid"]:
        raise SignatureError("from_kid is not the resolved key")
    verify_document(doc, [sender_sign_key], expected_type="envelope", required=[sender_sign_key.kid], now=n)
    iat = parse_time(p["iat"])
    if iat > n + MAX_SKEW:
        raise ValidationError("iat is in the future")
    if iat < n - ENVELOPE_MAX_AGE:
        raise ValidationError("envelope is older than 8 days")
    if p["kind"] not in P.KINDS:
        raise ValidationError(f"unknown kind {p['kind']!r}", "unknown_kind")
    priv = my_seal_keys.get(p["to_kid"])
    if priv is None:
        raise SealError("no key for to_kid")
    info = seal_info(p["from"], p["from_kid"], p["to"], p["to_kid"], p["id"])
    plain = open_sealed(priv, p["sealed"], info)
    try:
        body = json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationError("body is not valid JSON", "bad_body") from e
    P.validate_body(p["kind"], body)
    return p, body


# --- fingerprints and friend codes -----------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_FP = re.compile(r"[0-9A-HJKMNP-TV-Z]{16}\Z")
CODE_PREFIX = "l0wf1:"


def fingerprint(pub_raw: bytes) -> str:
    """First 10 bytes of SHA-256 of the raw signing public key, Crockford base32, `XXXX-XXXX-XXXX-XXXX`."""
    n = int.from_bytes(hashlib.sha256(pub_raw).digest()[:10], "big")
    s = "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(15, -1, -1))
    return "-".join(s[i:i + 4] for i in range(0, 16, 4))


def make_friend_code(address: str, fingerprint_: str, secret: bytes | None = None) -> tuple[str, bytes]:
    """-> (text, secret). `fingerprint_` may be dashed or not."""
    fp = fingerprint_.replace("-", "").upper()
    if not _FP.match(fp):
        raise ValidationError("bad fingerprint")
    name, domain = P.split_address(address)
    secret = os.urandom(16) if secret is None else secret
    if len(secret) != 16:
        raise ValidationError("secret must be 16 bytes")
    return f"{CODE_PREFIX}{name}@{domain}:{fp}:{b64.encode(secret)}", secret


def parse_friend_code(text: str) -> tuple[str, str, bytes]:
    """-> (address, fingerprint as `XXXX-XXXX-XXXX-XXXX`, secret). Raises ValidationError (code `bad_friend_code`)."""
    try:
        if not isinstance(text, str) or not text.startswith(CODE_PREFIX):
            raise ValueError("not a friend code")
        rest = text[len(CODE_PREFIX):]
        # the address may itself hold a colon (a port), so split from the right
        parts = rest.rsplit(":", 2)
        if len(parts) != 3:
            raise ValueError("wrong number of parts")
        address, fp, sec = parts
        if address != address.lower():
            raise ValueError("address must be lowercase")
        P.split_address(address)
        if not _FP.match(fp):
            raise ValueError("bad fingerprint")
        secret = b64.decode(sec)
        if len(secret) != 16:
            raise ValueError("secret must be 16 bytes")
    except (ValueError, ProtocolError) as e:
        raise ValidationError(f"bad friend code: {e}", "bad_friend_code") from e
    return address, "-".join(fp[i:i + 4] for i in range(0, 16, 4)), secret
