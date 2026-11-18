"""Keys and key ids (signing.md)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from . import b64
from .errors import KeyFormatError

USES = {"sign": "Ed25519", "recover": "Ed25519", "seal": "X25519"}
_RAW = serialization.Encoding.Raw
_RAWPUB = serialization.PublicFormat.Raw


def compute_kid(alg: str, pub: bytes) -> str:
    h = hashlib.sha256(alg.encode("ascii") + b"\n" + pub).digest()
    return "k1." + b64.encode(h[:16])


@dataclass(frozen=True)
class Key:
    """A public key object as carried in documents."""

    kid: str
    alg: str
    use: str
    pub: bytes
    created: str

    @property
    def usable(self) -> bool:
        """Unknown algs are unusable, not errors (signing.md)."""
        return self.alg in ("Ed25519", "X25519")

    def to_dict(self) -> dict:
        return {"kid": self.kid, "alg": self.alg, "use": self.use, "pub": b64.encode(self.pub), "created": self.created}

    @classmethod
    def from_dict(cls, d: dict) -> Key:
        """Parse and verify the kid. Raises KeyFormatError on mismatch."""
        if not isinstance(d, dict):
            raise KeyFormatError("key must be an object")
        for f in ("kid", "alg", "use", "pub", "created"):
            if not isinstance(d.get(f), str):
                raise KeyFormatError(f"key field {f!r} missing or not a string")
        pub = b64.decode(d["pub"])
        if d["alg"] in USES.values() and len(pub) != 32:
            raise KeyFormatError("public key must be 32 bytes")
        key = cls(d["kid"], d["alg"], d["use"], pub, d["created"])
        if not verify_kid(key):
            raise KeyFormatError("kid does not match the public key")
        return key

    def verify(self, signature: bytes, message: bytes) -> bool:
        if self.alg != "Ed25519":
            return False
        try:
            Ed25519PublicKey.from_public_bytes(self.pub).verify(signature, message)
            return True
        except (InvalidSignature, ValueError):
            return False


def verify_kid(key: Key) -> bool:
    return key.kid == compute_kid(key.alg, key.pub)


@dataclass(frozen=True)
class KeyPair:
    """A key with its private half (held by the gateway; test tools and fake hubs)."""

    key: Key
    private: bytes = field(repr=False)  # the 32-byte seed

    @property
    def kid(self) -> str:
        return self.key.kid

    def sign(self, message: bytes) -> bytes:
        if self.key.alg != "Ed25519":
            raise KeyFormatError("only Ed25519 keys sign")
        return Ed25519PrivateKey.from_private_bytes(self.private).sign(message)


def from_seed(seed: bytes, use: str = "sign", created: str = "2026-11-17T21:30:00Z") -> KeyPair:
    if len(seed) != 32:
        raise KeyFormatError("seed must be 32 bytes")
    if use not in USES:
        raise KeyFormatError(f"unknown use {use!r}")
    alg = USES[use]
    if alg == "Ed25519":
        pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(_RAW, _RAWPUB)
    else:
        pub = X25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(_RAW, _RAWPUB)
    return KeyPair(Key(compute_kid(alg, pub), alg, use, pub, created), seed)


def generate(use: str = "sign", created: str | None = None) -> KeyPair:
    import os

    from .times import format_time, now

    return from_seed(os.urandom(32), use, created or format_time(now()))
