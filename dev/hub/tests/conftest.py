import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from fakehub.config import Config
from fakehub.hub import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, tmp: Path, **cfg_kw):
        self.port = free_port()
        self.cfg = Config(base_url=f"http://hub.localhost:{self.port}", state_path=tmp / "state.json", listen_port=self.port,
                          **{"reserved": ["opsname"], **cfg_kw})
        self.app = create_app(self.cfg)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return self.cfg.base_url

    @property
    def connect(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("hub did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def running(tmp_path):
    r = Running(tmp_path).start()
    yield r
    r.stop()
