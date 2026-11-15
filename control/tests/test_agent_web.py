"""agent-web keeps the owner's credentials away from the agent's web page."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from .stack import REPO, free_port, wait_http


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, body: bytes) -> None:
        out = json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "l0w_session=planted; Path=/")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._reply(b"")

    def do_POST(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while (size := int(self.rfile.readline().split(b";")[0], 16)) > 0:
                body += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply(body)

    def log_message(self, *_):
        pass


@pytest.fixture
def fwd():
    up = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    port = free_port()
    p = subprocess.Popen([sys.executable, str(REPO / "agent-web" / "fwd.py")], env={"LISTEN_HOST": "127.0.0.1", "LISTEN_PORT": str(port),
                         "TARGET_HOST": "127.0.0.1", "TARGET_PORT": str(up.server_address[1]), "PATH": "/usr/bin:/bin"})
    wait_http(f"http://127.0.0.1:{port}/")
    yield f"http://127.0.0.1:{port}"
    p.kill()
    up.shutdown()


def test_credentials_never_reach_the_agent(fwd):
    with httpx.Client(base_url=fwd, cookies={"l0w_session": "owner-secret"}, headers={"Authorization": "Bearer device-token"}) as c:
        for i in range(3):  # several requests on one kept-alive connection
            r = c.get(f"/page{i}", headers={"Cookie": "l0w_session=owner-secret"})
            seen = r.json()
            assert seen["path"] == f"/page{i}"
            assert "cookie" not in seen["headers"] and "authorization" not in seen["headers"]
            assert "set-cookie" not in r.headers
        r = c.post("/form", content=b"a=1&b=2")
        assert r.json()["body"] == "a=1&b=2" and "cookie" not in r.json()["headers"]
        r = c.post("/chunked", content=iter([b"hello ", b"world"]))
        assert r.json()["body"] == "hello world"
        assert "cookie" not in r.json()["headers"]
    assert "owner-secret" not in json.dumps(seen)
