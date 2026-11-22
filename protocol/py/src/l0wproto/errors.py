"""Errors raised by the library. Each carries the protocol error `code` where one applies."""

from __future__ import annotations

from .consts import VERSION


class ProtocolError(Exception):
    code = "bad_request"

    def __init__(self, detail: str = "", code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code

    def body(self) -> dict:
        return error_body(self.code, self.detail)


class EncodingError(ProtocolError):
    code = "bad_encoding"


class KeyFormatError(ProtocolError):
    code = "bad_key"


class SignatureError(ProtocolError):
    code = "bad_signature"


class DocumentError(ProtocolError):
    code = "bad_document"


class UnsupportedVersion(DocumentError):
    code = "unsupported_version"


class ValidationError(DocumentError):
    code = "bad_document"


class NameRuleError(ProtocolError):
    code = "bad_registration"


class InviteError(ProtocolError):
    code = "bad_invite"


class FrameError(ProtocolError):
    code = "bad_frame"


class SealError(ProtocolError):
    """Sealing or opening failed (HPKE). The detail never says why an open failed."""

    code = "bad_seal"


class HopAuthError(ProtocolError):
    """Hop-auth failure. `reason` is for logs only; never send it to the caller."""

    code = "bad_signature"

    def __init__(self, reason: str = ""):
        super().__init__("")
        self.reason = reason


def error_body(code: str, detail: str = "") -> dict:
    return {"v": VERSION, "error": code, "detail": detail}
