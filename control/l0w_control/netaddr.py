"""Who is really calling: the client address and scheme behind a reverse proxy or tunnel.

Forwarding headers are believed only when the socket peer is in L0W_TRUSTED_PROXIES
(comma-separated addresses or subnets; empty by default, so nothing is trusted).
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache

from starlette.requests import Request


@lru_cache(maxsize=8)
def _parse_networks(raw: str) -> tuple:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def trusted_networks() -> tuple:
    return _parse_networks(os.environ.get("L0W_TRUSTED_PROXIES", ""))


def _ip(text: str):
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _in(nets: tuple, addr) -> bool:
    return addr is not None and any(addr.version == n.version and addr in n for n in nets)


def peer_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _peer_trusted(request: Request) -> bool:
    nets = trusted_networks()
    return bool(nets) and _in(nets, _ip(peer_ip(request)))


def client_ip(request: Request) -> str:
    """The caller's address. From a trusted proxy: CF-Connecting-IP, else the right-most
    X-Forwarded-For hop that isn't itself a trusted proxy; otherwise the socket peer."""
    peer = peer_ip(request)
    if not _peer_trusted(request):
        return peer
    cf = _ip(request.headers.get("cf-connecting-ip", ""))
    if cf is not None:
        return str(cf)
    nets = trusted_networks()
    hops = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        addr = _ip(hop)
        if addr is None:
            break  # garbage: stop believing the chain
        if not _in(nets, addr):
            return str(addr)
    return peer


def request_scheme(request: Request) -> str:
    """http or https, believing X-Forwarded-Proto only from a trusted proxy."""
    if _peer_trusted(request):
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto in ("http", "https"):
            return proto
    return request.url.scheme


def is_local_or_private(addr: str) -> bool:
    """Loopback or private (RFC 1918 / ULA / link-local). Unparseable (a test client) counts."""
    ip = _ip(addr)
    if ip is None:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local
