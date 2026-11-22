"""Snapshot GET responses from the mock backend for the web app's render tests.

    uv run python scripts/dump_mock_responses.py   → web/src/test/mock-responses.json
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from l0w_control.app import create_app
from l0w_control.mock.backend import MockBackend

OUT = Path(__file__).resolve().parents[2] / "web" / "src" / "test" / "mock-responses.json"

GETS = [
    "/api/meta", "/api/auth/me", "/api/status", "/api/membership", "/api/inbox/counts", "/api/inbox?view=waiting", "/api/inbox?view=history",
    "/api/claims?active=true", "/api/jobs", "/api/messages/recent?sender=assistant&limit=5", "/api/projects", "/api/projects?status=active",
    "/api/projects/garden-irrigation", "/api/projects/garden-irrigation/shelf", "/api/threads", "/api/threads?project=garden-irrigation&archived=true",
    "/api/threads/1", "/api/threads/1/items?limit=200", "/api/threads/1/delivery", "/api/runs", "/api/runs/123",
    "/api/runs/123/timeline?view=simple", "/api/runs/123/timeline?view=detailed", "/api/files/list?volume=workspace&path=",
    "/api/account/sessions", "/api/system", "/api/secrets", "/api/budget", "/api/calendar", "/api/routines", "/api/providers", "/api/roles",
    "/api/reviewer", "/api/instructions", "/api/system/update", "/api/notifications/settings", "/api/network",
    "/api/integrations", "/api/integrations/presets", "/api/connected-apps",
    "/api/friends", "/api/friends/requests", "/api/friends/settings", "/api/friends/fingerprint", "/api/friends/codes", "/api/friends/breaker",
]
POSTS = ["/api/system/selftest"]  # the render tests answer by path, whatever the method

if __name__ == "__main__":
    c = TestClient(create_app(MockBackend()))
    out = {}
    for url in GETS:
        r = c.get(url)
        r.raise_for_status()
        out[url] = r.json()
    for url in POSTS:
        r = c.post(url)
        r.raise_for_status()
        out[url] = r.json()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {len(out)} responses to {OUT}")
