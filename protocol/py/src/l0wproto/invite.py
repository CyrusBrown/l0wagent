"""`l0w-invite:<hub base URL>#<code>` (membership.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import InviteError

PREFIX = "l0w-invite:"
_CODE = re.compile(r"[A-Za-z0-9_.~-]+\Z")


@dataclass(frozen=True)
class Invite:
    base_url: str  # scheme://host[:port], no trailing slash
    code: str

    @property
    def domain(self) -> str:
        return urlsplit(self.base_url).netloc


def parse_invite(s: str, *, allow_insecure: bool = False) -> Invite:
    """`allow_insecure` permits http:// (local development only; the protocol is HTTPS)."""
    if not isinstance(s, str) or not s.startswith(PREFIX):
        raise InviteError("not an l0w-invite string")
    rest = s[len(PREFIX):]
    url, sep, code = rest.partition("#")
    if not sep or not code or "#" in code:
        raise InviteError("invite has no code")
    if not _CODE.match(code):
        raise InviteError("invite code has invalid characters")
    try:
        u = urlsplit(url)
        host = u.hostname
        u.port  # noqa: B018  (raises on a bad port)
    except ValueError as e:
        raise InviteError("bad hub URL") from e
    if u.scheme not in (("https", "http") if allow_insecure else ("https",)) or not host:
        raise InviteError("hub URL must be https")
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        raise InviteError("hub URL must be a base URL (scheme and host only)")
    return Invite(f"{u.scheme}://{u.netloc.lower()}", code)


def format_invite(base_url: str, code: str) -> str:
    return f"{PREFIX}{base_url.rstrip('/')}#{code}"
