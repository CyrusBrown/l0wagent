"""Hop authentication: RFC 9421 HTTP Message Signatures with Ed25519, RFC 9530 Content-Digest (hop-auth.md)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import b64
from .consts import MAX_SKEW, SUPPORTED_VERSIONS, VERSION
from .errors import HopAuthError, ValidationError
from .keys import Key, KeyPair
from .payloads import split_address
from .times import now as _now

LABEL = "l0w"
TAG = VERSION
ALG = "ed25519"


# --- nonce cache -----------------------------------------------------------------------------


class NonceCache:
    """Interface: remember (keyid, nonce) until `expires` (Unix seconds)."""

    def add_if_new(self, keyid: str, nonce: str, expires: int) -> bool:
        """Record the pair and return True, or return False if it was already seen and not yet expired."""
        raise NotImplementedError


class InMemoryNonceCache(NonceCache):
    def __init__(self, clock: Callable[[], int] = _now):
        self._seen: dict[tuple[str, str], int] = {}
        self._clock = clock

    def add_if_new(self, keyid: str, nonce: str, expires: int) -> bool:
        now = self._clock()
        self._seen = {k: e for k, e in self._seen.items() if e >= now}
        if (keyid, nonce) in self._seen:
            return False
        self._seen[(keyid, nonce)] = expires
        return True


# --- building --------------------------------------------------------------------------------


def content_digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii") + ":"


def covered_components(has_body: bool) -> list[str]:
    return ["@method", "@target-uri"] + (["content-digest", "content-type"] if has_body else [])


def signature_params(components: list[str], created: int, expires: int, nonce: str, keyid: str) -> str:
    inner = " ".join(f'"{c}"' for c in components)
    return f'({inner});created={created};expires={expires};nonce="{nonce}";keyid="{keyid}";alg="{ALG}";tag="{TAG}"'


def signature_base(method: str, target_uri: str, content_digest_value: str | None, content_type: str | None, params: str,
                   components: list[str] | None = None) -> str:
    comps = components or covered_components(content_digest_value is not None)
    values = {"@method": method.upper(), "@target-uri": target_uri, "content-digest": content_digest_value,
              "content-type": content_type}
    lines = [f'"{c}": {values[c]}' for c in comps]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines)


@dataclass(frozen=True)
class SignedRequest:
    headers: dict[str, str]  # to add to the request
    signature_base: str
    params: str


def sign_request(method: str, target_uri: str, body: bytes, content_type: str | None, signer: KeyPair, address: str, *,
                 created: int | None = None, ttl: int = MAX_SKEW, nonce: str | None = None) -> SignedRequest:
    has_body = bool(body)
    if has_body and not content_type:
        raise ValidationError("a request with a body must have a content type")
    if not 0 < ttl <= MAX_SKEW:
        raise ValidationError("expires may be at most 300 s after created")
    created = _now() if created is None else created
    nonce = nonce or b64.encode(os.urandom(16))
    comps = covered_components(has_body)
    params = signature_params(comps, created, created + ttl, nonce, signer.kid)
    cd = content_digest(body) if has_body else None
    base = signature_base(method, target_uri, cd, content_type if has_body else None, params, comps)
    sig = base64.b64encode(signer.sign(base.encode("utf-8"))).decode("ascii")
    headers = {"Signature-Input": f"{LABEL}={params}", "Signature": f"{LABEL}=:{sig}:", "L0W-Signer": address}
    if has_body:
        headers["Content-Digest"] = cd
    return SignedRequest(headers, base, params)


# --- verifying -------------------------------------------------------------------------------

_INPUT = re.compile(r'l0w=(\((?:"[^"]*"(?: "[^"]*")*)?\))((?:;[a-z]+=(?:\d+|"[^"]*"))*)\Z')
_PARAM = re.compile(r';([a-z]+)=(\d+|"[^"]*")')
_SIG = re.compile(r"l0w=:([A-Za-z0-9+/]+={0,2}):\Z")


@dataclass(frozen=True)
class HopResult:
    address: str
    keyid: str
    nonce: str
    created: int
    expires: int


def verify_request(method: str, target_uri: str, headers: Mapping[str, str], body: bytes,
                   resolve_key: Callable[[str, str], Key | None], nonces: NonceCache, now: int | None = None,
                   supported_tags: tuple[str, ...] = SUPPORTED_VERSIONS) -> HopResult:
    """Verify a hop-authenticated request. Raises HopAuthError (answer `401 bad_signature`, no detail).

    `resolve_key(address, keyid)` returns the signer's current, non-revoked signing key from its directory
    entry (cache/refetch is the caller's business) or None.
    """
    n = _now() if now is None else now
    h = {k.lower(): v for k, v in headers.items()}

    def bad(why: str):
        return HopAuthError(why)

    inp = _INPUT.match(h.get("signature-input", "").strip())
    sigm = _SIG.match(h.get("signature", "").strip())
    if not inp or not sigm:
        raise bad("missing or malformed signature headers")
    inner, rest = inp.group(1), inp.group(2)
    params_raw = inner + rest
    comps = re.findall(r'"([^"]*)"', inner)
    pairs = _PARAM.findall(rest)
    p = {}
    for k, v in pairs:
        if k in p:
            raise bad("duplicate parameter")
        p[k] = int(v) if v.isdigit() else v[1:-1]
    try:
        created, expires = p["created"], p["expires"]
        nonce, keyid, alg, tag = p["nonce"], p["keyid"], p["alg"], p["tag"]
    except KeyError as e:
        raise bad(f"missing parameter {e}") from e
    if not all(isinstance(x, int) for x in (created, expires)) or not all(isinstance(x, str) for x in (nonce, keyid, alg, tag)):
        raise bad("parameter types")
    if tag not in supported_tags or alg != ALG:
        raise bad("tag or alg")
    if created > n + MAX_SKEW or expires < n or expires <= created or expires - created > MAX_SKEW:
        raise bad("time window")
    has_body = bool(body)
    if comps != covered_components(has_body):
        raise bad("covered components")
    signer = h.get("l0w-signer", "")
    try:
        split_address(signer)
    except ValidationError as e:
        raise bad("signer address") from e
    key = resolve_key(signer.lower(), keyid)
    if key is None or key.kid != keyid or key.use != "sign" or key.alg != "Ed25519":
        raise bad("unknown key")
    cd = None
    ctype = None
    if has_body:
        cd = h.get("content-digest")
        if cd != content_digest(body):
            raise bad("digest")
        ctype = h.get("content-type")
        if not ctype:
            raise bad("content-type")
    base = signature_base(method, target_uri, cd, ctype, params_raw, comps)
    try:
        sig = base64.b64decode(sigm.group(1), validate=True)
    except (binascii.Error, ValueError) as e:
        raise bad("signature encoding") from e
    if len(sig) != 64 or not key.verify(sig, base.encode("utf-8")):
        raise bad("signature")
    if not nonces.add_if_new(keyid, nonce, expires):
        raise bad("replayed nonce")
    return HopResult(signer.lower(), keyid, nonce, created, expires)
