"""Isolation probes, run from inside the agent container.

Used by the runner's /selftest (the UI's self-test) and by `dev/isolation-test.sh`
(`python3 selftest.py '<targets json>'`). Each probe checks something the agent must NOT
be able to do. Because the container is untrusted, a passing result means "configured
correctly", not "can't be tampered with"; enforcement lives in control and egress.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 3.0


def _connect(hostport: str) -> str | None:
    """None if a TCP connection succeeded, else why it failed."""
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=TIMEOUT):
            return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def _http_status(url: str) -> tuple[int | None, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # direct, never via egress
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=TIMEOUT) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return None, str(getattr(e, "reason", e))


def _proxy_connect(proxy: str, target: str) -> tuple[int | None, str]:
    """Send CONNECT target through the HTTP proxy; return the proxy's status code."""
    host, _, port = proxy.removeprefix("http://").rstrip("/").rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=TIMEOUT * 2) as sock:
            sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            head = sock.recv(4096).decode("latin-1")
        return int(head.split(" ", 2)[1]), head.splitlines()[0] if head else ""
    except (OSError, ValueError, IndexError) as e:
        return None, str(e)


def _caps() -> set[str]:
    names = {12: "NET_ADMIN", 13: "NET_RAW", 25: "SYS_TIME", 16: "SYS_MODULE", 19: "SYS_PTRACE"}
    try:
        line = next(ln for ln in Path("/proc/self/status").read_text().splitlines() if ln.startswith("CapBnd:"))
    except (OSError, StopIteration):
        return set()
    mask = int(line.split()[1], 16)
    return {name for bit, name in names.items() if mask & (1 << bit)}


def run(t: dict) -> list[dict]:
    checks: list[dict] = []

    def add(id_: str, label: str, result: str, detail: str = "") -> None:
        checks.append({"id": id_, "label": label, "result": result, "detail": detail or None})

    reached = [hp for hp in t.get("direct_internet", ["1.1.1.1:443", "8.8.8.8:53"]) if _connect(hp) is None]
    add("no_direct_internet", "No direct internet (only through egress)", "fail" if reached else "ok", f"reached {', '.join(reached)}" if reached else "")

    reached = [hp for hp in t.get("lan", []) if _connect(hp) is None]
    add("no_lan", "Can't reach the LAN or the host", "fail" if reached else "ok", f"reached {', '.join(reached)}" if reached else f"checked {len(t.get('lan', []))} addresses")

    if t.get("control_owner"):
        code, err = _http_status(t["control_owner"])
        result = "ok" if code == 403 else "warn" if code == 401 else "fail" if code and code < 400 else "ok" if code is None else "warn"
        add("owner_api_refused", "Control's owner API refuses the agent", result, f"HTTP {code}" if code else err)

    if t.get("egress_admin"):
        code, err = _http_status(t["egress_admin"])
        add("egress_admin_refused", "Egress admin API refuses the agent", "ok" if code in (None, 401, 403) else "fail", f"HTTP {code}" if code else err)

    proxy = t.get("proxy")
    if proxy and t.get("gated_host"):
        if t.get("lease_active"):
            add("gated_without_lease", "Model provider blocked without a lease", "skip", "a run holds the lease right now")
        else:
            code, line = _proxy_connect(proxy, f"{t['gated_host']}:443")
            add("gated_without_lease", "Model provider blocked without a lease", "ok" if code == 403 else "fail", line)
    if proxy:
        code, line = _proxy_connect(proxy, t.get("internet_probe", "example.com:443"))
        add("internet_via_egress", "Internet works through egress", "ok" if code == 200 else "warn", line)

    caps = _caps() & {"NET_ADMIN", "NET_RAW"}
    add("no_net_caps", "No NET_ADMIN or NET_RAW", "fail" if caps else "ok", ", ".join(sorted(caps)))

    sock = next((p for p in ("/var/run/docker.sock", "/run/docker.sock") if os.path.exists(p)), None)
    add("no_docker_socket", "No Docker socket", "fail" if sock else "ok", sock or "")
    return checks


if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}), indent=1))
