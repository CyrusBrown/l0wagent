"""RFC 3339 UTC times with a Z and whole seconds (README rules)."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

from .consts import MAX_SKEW
from .errors import ValidationError

_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def format_time(epoch: int | float | datetime) -> str:
    if isinstance(epoch, datetime):
        epoch = epoch.timestamp()
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> int:
    if not isinstance(s, str) or not _RE.match(s):
        raise ValidationError(f"bad time {s!r}: want YYYY-MM-DDTHH:MM:SSZ")
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
    except ValueError as e:
        raise ValidationError(f"bad time {s!r}") from e


def now() -> int:
    return int(time.time())


def check_fresh(iat: str, now_: int | None = None, skew: int = MAX_SKEW) -> None:
    """`iat` within `skew` seconds of now, either direction (registration, tunnel-hello)."""
    t = parse_time(iat)
    n = now() if now_ is None else now_
    if abs(n - t) > skew:
        raise ValidationError("iat outside allowed clock skew")
