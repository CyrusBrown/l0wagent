"""base64url, no padding, strict decode (signing.md)."""

from __future__ import annotations

import base64
import re

from .errors import EncodingError

_ALPHABET = re.compile(r"[A-Za-z0-9_-]*\Z")


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode(s: str) -> bytes:
    """Reject padding, characters outside the alphabet, and non-canonical trailing bits."""
    if not isinstance(s, str):
        raise EncodingError("base64url value must be a string")
    if not _ALPHABET.match(s):
        raise EncodingError("invalid base64url character (padding is not allowed)")
    if len(s) % 4 == 1:
        raise EncodingError("invalid base64url length")
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if encode(raw) != s:
        raise EncodingError("non-canonical base64url")
    return raw
