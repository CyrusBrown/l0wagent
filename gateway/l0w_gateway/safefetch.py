"""Vetting for the gateway's own outgoing requests (a friend's directory and inbox).

The gateway sits on the stack's internal network, and a friend's address names a host somebody else
chose, so every target is vetted like egress does: https only, the name resolves only to public
addresses, the stack's own services are refused, and the request connects to the address that was
vetted (the Host header and TLS name stay the original). Redirects are never followed by callers.
Ported from control/l0w_control/safefetch.py (the gateway can't import control); it has no
"allow private" option: development uses the `L0W_HUB_ALLOW_HTTP` / `L0W_HUB_CONNECT_TO` flags
(see peers.Net), which skip vetting altogether.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

# The compose services and networks (compose.yaml), plus names the host side gives itself.
STACK_NAMES = frozenset({
    "control", "egress", "egress-admin", "agent", "runner", "chat-runtime", "web", "sandbox", "chat", "ctl", "outside",
    "gateway", "tunnel", "control-hub", "hubnet",
    "localhost", "host.docker.internal", "gateway.docker.internal",
})


class VetError(Exception):
    """The URL is not one the gateway will call. The message says why."""


@dataclass(frozen=True)
class Vetted:
    url: str
    scheme: str
    host: str
    port: int
    ip: str


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


def vet_url(url: str) -> Vetted:
    """Check `url` and return the address to connect to. Raises VetError if it isn't allowed."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower().rstrip("."), parts.port
    except ValueError:
        raise VetError("that URL isn't valid") from None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise VetError("the URL must be https:// with no user name or password")
    if host in STACK_NAMES:
        raise VetError(f"{host} is one of this install's own services")
    port = port or 443
    try:
        literal = [str(_addr(host))]
    except ValueError:
        literal = None
    ips = literal or resolve(host, port)
    if not ips:
        raise VetError(f"couldn't find {host}")
    addrs = [_addr(i) for i in ips]
    if any(_forbidden(a) for a in addrs):
        raise VetError(f"{host} points at an address this server won't call (loopback, link-local or similar)")
    if any(str(a) in _stack_ips() for a in addrs):
        raise VetError(f"{host} is one of this install's own services")
    if any(not a.is_global for a in addrs):
        raise VetError(f"{host} is on a private network")
    return Vetted(url, parts.scheme, host, port, str(addrs[0]))


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Vets each request's URL and connects to the vetted address, keeping Host and TLS name."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            v = await asyncio.to_thread(vet_url, str(request.url))
        except VetError as e:
            raise httpx.ConnectError(str(e)) from None
        host_header = request.headers.get("host") or (v.host if request.url.port is None else f"{v.host}:{v.port}")
        request.url = request.url.copy_with(host=v.ip)
        request.headers["Host"] = host_header
        request.extensions = {**request.extensions, "sni_hostname": v.host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
