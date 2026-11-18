"""Settings for the fake hub, from keyword arguments, environment variables (FAKEHUB_*) or the CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_LIMITS = {"tunnel_streams": 64, "request_body_bytes": 104857600, "auth_rate_per_minute": 20}


@dataclass
class Config:
    base_url: str = "http://hub.localhost:8700"
    #: the hub domain in addresses (`name@domain`); the host part of base_url, without the port
    domain: str = ""
    #: parent domain of files hostnames (a different site from `domain`)
    files_domain: str = "hubfiles.localhost"
    name: str = "Fake hub"
    state_path: Path = Path("fakehub-state.json")
    #: file holding the hub signing key's 32-byte seed (hex); created on first start
    seed_path: Path | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 0  # 0: take it from base_url
    min_release: str = "0.8.0"
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    invite_days: int = 7
    #: extra reserved names besides the hub's own (the equivalent of the Cloudflare hub's RESERVED_NAMES)
    reserved: list = field(default_factory=list)
    #: tunnel liveness: PING after this many seconds of silence; dead after dead_after
    ping_interval: float = 30.0
    dead_after: float = 90.0
    #: how long a replaced connection may keep serving its open streams before it is closed 4409
    drain_seconds: float = 30.0
    handshake_timeout: float = 10.0
    #: own-url mode: verify TLS when checking the member's URLs
    own_url_verify_tls: bool = True

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        u = urlsplit(self.base_url)
        if not self.domain:
            self.domain = (u.hostname or "hub.localhost").lower()
        self.state_path = Path(self.state_path)
        if self.seed_path is None:
            self.seed_path = self.state_path.with_suffix(".key")
        self.seed_path = Path(self.seed_path)
        if not self.listen_port:
            self.listen_port = u.port or (443 if u.scheme == "https" else 80)

    @property
    def port_suffix(self) -> str:
        u = urlsplit(self.base_url)
        return f":{u.port}" if u.port else ""

    @property
    def api(self) -> str:
        return f"{self.base_url}/v0/"

    @property
    def tunnel_url(self) -> str:
        u = urlsplit(self.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return f"{scheme}://{u.netloc}/v0/tunnel"

    def hostnames_for(self, name: str) -> dict:
        """Two separate parent domains, both under .localhost in development. The port stays in the
        hostname so a browser reaches the dev server (a real hub's hostnames have none)."""
        s = self.port_suffix
        return {"instance": f"{name}.{self.domain}{s}", "files": f"files--{name}.{self.files_domain}{s}"}

    def reserved_extra(self) -> list[str]:
        """The hub's own name and its services (membership.md)."""
        return [self.domain.split(".")[0], *self.reserved]

    @classmethod
    def from_env(cls, **overrides) -> Config:
        env = os.environ.get
        kw: dict = {}
        for key, var in (("base_url", "FAKEHUB_BASE_URL"), ("domain", "FAKEHUB_DOMAIN"), ("files_domain", "FAKEHUB_FILES_DOMAIN"),
                         ("name", "FAKEHUB_NAME"), ("state_path", "FAKEHUB_STATE"), ("seed_path", "FAKEHUB_KEY"),
                         ("listen_host", "FAKEHUB_LISTEN")):
            if env(var):
                kw[key] = env(var)
        if env("FAKEHUB_RESERVED"):
            kw["reserved"] = [n.strip().lower() for n in env("FAKEHUB_RESERVED").split(",") if n.strip()]
        if env("FAKEHUB_PORT"):
            kw["listen_port"] = int(env("FAKEHUB_PORT"))
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)
