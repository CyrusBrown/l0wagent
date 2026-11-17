"""Links control stores or shows that came from the agent or a chat model."""

from __future__ import annotations

from urllib.parse import urlsplit

SAFE_SCHEMES = ("http", "https", "l0wagent")


def safe_url(url: object, max_len: int = 500) -> str | None:
    """The URL if it's an http, https or l0wagent link (a host or path follows), else None."""
    u = str(url or "").strip()
    if not u or len(u) > max_len or any(c in u for c in "\r\n\t\x00 "):
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme.lower() not in SAFE_SCHEMES or not (parts.netloc or parts.path):
        return None
    return u


def clean_links(links: list[dict]) -> list[dict]:
    """Project links as {label, url}; refuses a link whose URL isn't safe."""
    from .backends.base import BadRequest

    out = []
    for x in links:
        url = safe_url(x.get("url"))
        if url is None:
            raise BadRequest("bad_link", f"Links must be http://, https:// or l0wagent:// URLs, not {str(x.get('url', ''))[:60]!r}.")
        out.append({"label": str(x.get("label", ""))[:80], "url": url})
    return out
