"""Vetting for URLs control calls on its own (integrations, ntfy; later the hub's gateway).

Control sits on the internal networks and isn't behind egress, so a URL someone typed (or a chat
model proposed) must not be able to reach the stack's own services or the host. `vet_url` resolves
the host, refuses anything internal and returns the address to connect to. `PinnedTransport` is an
httpx transport that does this on every request and connects to the vetted address (the Host header
and TLS name stay the original host), so a name can't resolve differently between check and use.
Redirects are never followed by callers.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .backends.base import BadRequest

# The compose services and networks (compose.yaml), plus names the host side gives itself.
STACK_NAMES = frozenset({
    "control", "egress", "egress-admin", "agent", "runner", "chat-runtime", "web", "sandbox", "chat", "ctl", "outside",
    "localhost", "host.docker.internal", "gateway.docker.internal",
})


@dataclass(frozen=True)
class Vetted:
    url: str
    scheme: str
    host: str
    port: int
    ip: str
    private: bool


def _getaddrinfo(host: str, port: int) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        return []


resolve = _getaddrinfo  # tests replace this


def _addr(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    a = ipaddress.ip_address(text.split("%")[0])
    return a.ipv4_mapped if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped else a


def _forbidden(a) -> bool:
    return a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast or a.is_reserved


_stack_cache: tuple[float, set[str]] = (0.0, set())


def _stack_ips() -> set[str]:
    global _stack_cache
    if time.monotonic() - _stack_cache[0] > 30:
        out: set[str] = set()
        for n in STACK_NAMES:
            for ip in resolve(n, 80):
                out.add(str(_addr(ip)))
        _stack_cache = (time.monotonic(), out)
    return _stack_cache[1]


def vet_url(url: str, allow_private: bool = False) -> Vetted:
    """Check `url` and return the address to connect to. Raises BadRequest if it isn't allowed.

    https is required, except for an address on a private network when `allow_private` is set.
    Loopback, link-local, unspecified and multicast addresses, and the stack's own services, are
    never allowed."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower().rstrip("."), parts.port
    except ValueError:
        raise BadRequest("bad_url", "That URL isn't valid.") from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise BadRequest("bad_url", "The URL starts with https:// (or http:// on your own network) and has no user name or password.")
    if host in STACK_NAMES:
        raise BadRequest("bad_url", f"{host} is one of this install's own services.")
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        literal = [str(_addr(host))]
    except ValueError:
        literal = None
    ips = literal or resolve(host, port)
    if not ips:
        raise BadRequest("bad_url", f"Couldn't find {host}.")
    addrs = [_addr(i) for i in ips]
    for a in addrs:
        if _forbidden(a):
            raise BadRequest("bad_url", f"{host} points at an address this server won't call (loopback, link-local or similar).")
    own = _stack_ips()
    if any(str(a) in own for a in addrs):
        raise BadRequest("bad_url", f"{host} is one of this install's own services.")
    private = all(not a.is_global for a in addrs)
    if any(not a.is_global for a in addrs) and not (allow_private and private):
        raise BadRequest("bad_url", f"{host} is on a private network. Tick \"this is on my network\" to allow it.")
    if parts.scheme == "http" and not (allow_private and private):
        raise BadRequest("bad_url", "The URL must start with https:// (http:// is only for your own network, with \"this is on my network\" ticked).")
    return Vetted(url, parts.scheme, host, port, str(addrs[0]), private)


class PinnedTransport(httpx.BaseTransport):
    """Vets each request's URL and connects to the vetted address, keeping Host and TLS name."""

    def __init__(self, allow_private: bool = False, inner: httpx.BaseTransport | None = None):
        self.allow_private = allow_private
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            v = vet_url(str(request.url), self.allow_private)
        except BadRequest as e:
            raise httpx.ConnectError(e.detail or e.error) from None
        host_header = request.headers.get("host") or (v.host if request.url.port is None else f"{v.host}:{v.port}")
        ip = f"[{v.ip}]" if ":" in v.ip else v.ip
        request.url = request.url.copy_with(host=ip.strip("[]"))
        request.headers["Host"] = host_header
        request.extensions = {**request.extensions, "sni_hostname": v.host}
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, allow_private: bool = False, inner: httpx.AsyncBaseTransport | None = None):
        self.allow_private = allow_private
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import asyncio

        try:
            v = await asyncio.to_thread(vet_url, str(request.url), self.allow_private)
        except BadRequest as e:
            raise httpx.ConnectError(e.detail or e.error) from None
        host_header = request.headers.get("host") or (v.host if request.url.port is None else f"{v.host}:{v.port}")
        request.url = request.url.copy_with(host=v.ip)
        request.headers["Host"] = host_header
        request.extensions = {**request.extensions, "sni_hostname": v.host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
