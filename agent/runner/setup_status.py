"""Stands in for the runner while the container sets itself up (entrypoint.sh: the egress wait, the agent's
provision.sh after an update, its boot.sh). Every request gets `503 {"error": "setting_up", ...}`, so control
can tell "still setting up" from "down": runs wait instead of failing, and the UI says what's happening.

Standard library only. The entrypoint writes the current phase to PHASE_FILE and stops this before it starts
the runner on the same port.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PHASE_FILE = os.environ.get("SETUP_PHASE_FILE", "/run/assistant/setup-phase")
SINCE = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def phase() -> str:
    try:
        with open(PHASE_FILE) as f:
            return f.read().strip() or "starting"
    except OSError:
        return "starting"


class Handler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        body = json.dumps({"error": "setting_up", "phase": phase(), "since": SINCE}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "10")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer  # noqa: N815

    def log_message(self, *args) -> None:  # quiet: control asks every few seconds
        pass


def main() -> None:
    host, port = os.environ.get("RUNNER_HOST", "0.0.0.0"), int(os.environ.get("RUNNER_PORT", "8765"))
    for _ in range(30):  # a previous runner may still hold the port for a moment
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError:
            time.sleep(1)
    else:
        return
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
