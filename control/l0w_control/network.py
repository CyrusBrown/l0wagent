"""The Network page: egress's rules and request log, shaped for the owner.

Egress keeps the rules and the log (its own database); control only reads them and adds or
removes rules for the owner. A rule's hit count is worked out here from egress's per-host
stats, with the same matching egress uses for hosts (exact, `*.domain`, `host:port`); CIDR
rules show no count.
"""

from __future__ import annotations

import re

from .api import schemas as s
from .auth import parse
from .backends.base import BadRequest

PATTERN_RE = re.compile(r"^(\*|(\*\.)?[A-Za-z0-9.\-]+(/\d{1,2})?(:\d{1,5})?)$")
# The log's filter pills → egress's query parameters ("hour" adds `since`).
FILTERS: dict[str, dict] = {"all": {}, "blocked": {"action": "blocked"}, "get": {"method": "GET"}, "post": {"method": "POST"}, "hour": {}}


def check_pattern(pattern: str) -> str:
    pattern = pattern.strip().lower()
    if not PATTERN_RE.match(pattern):
        raise BadRequest("bad_pattern", "Use a host (api.example.com), *.example.com, host:port, or a CIDR like 192.168.1.0/24.")
    return pattern


def matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower(), (host or "").lower()
    if pattern == "*":
        return True
    pattern = pattern.split(":", 1)[0]
    if "/" in pattern:
        return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def view(status: dict, rules: list[dict], requests: list[dict], n24: int, b24: int, hosts1: list[dict], hosts7: list[dict]) -> s.NetworkView:
    out_rules = [s.EgressRule(id=r["id"], pattern=r["pattern"], action=r["action"], note=r.get("note") or "", created_at=parse(r.get("created_at")),
                              hits_7d=sum(h["n"] for h in hosts7 if matches(r["pattern"], h["host"]))) for r in rules]
    out_reqs = [s.EgressRequest(id=r["id"], at=parse(r["ts"]), method=r.get("method"), host=r.get("host"), port=r.get("port"), url=r.get("url"),  # type: ignore[arg-type]
                                action=r.get("action") or "", reason=r.get("reason"), status=r.get("status"), bytes_down=r.get("bytes_down") or 0)
                for r in requests]
    top = [s.HostStat(host=h["host"], requests=h["n"], blocked=h.get("blocked") or 0) for h in hosts1 if h.get("host")][:6]
    policy = "deny" if status.get("default_policy") == "deny" else "allow"
    return s.NetworkView(reachable=True, default_policy=policy, rules=out_rules, requests=out_reqs, requests_24h=n24, blocked_24h=b24, top_hosts=top)
