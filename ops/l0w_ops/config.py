"""Settings, read from the environment (compose.yaml sets them)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


@dataclass
class Settings:
    token: str                                   # L0W_OPS_TOKEN: control's
    docker_token: str = ""                       # L0W_OPS_DOCKER_TOKEN: ops -> ops-docker
    extensions_dir: Path = Path("/extensions")   # rw: the owner's extensions folder (also holds .ops.toml, .cache/, .l0w-update.json)
    toml_file: Path = Path("/project/extensions.toml")  # the owner's hand-written file, read-only here
    channel: str = "stable"                      # L0W_VERSION from .env: stable, edge or a pinned X.Y.Z
    profiles: str = ""                           # COMPOSE_PROFILES from .env: ops-docker is on when it's listed
    docker_url: str = "http://ops-docker:8796"
    github_api: str = "https://api.github.com"
    github_repo: str = "CyrusBrown/l0wagent"
    home: Path = Path("/tmp")                    # HOME for git (a tmpfs)
    # Test-only switches; never set in compose.yaml.
    allow_file_git: bool = False                 # accept file:// URLs
    allow_private_hosts: bool = False            # accept URLs that resolve to private addresses (a gitea on the LAN)
    git_env: dict = field(default_factory=dict)  # extra environment for git (tests map an https URL onto a local repository)
    limits: dict = field(default_factory=lambda: {"bytes": 100 * 1024 * 1024, "files": 20000, "git_seconds": 300})

    @property
    def docker_enabled(self) -> bool:
        return "ops-docker" in [p.strip() for p in self.profiles.split(",")]

    @property
    def ops_toml(self) -> Path:
        return self.extensions_dir / ".ops.toml"

    @property
    def update_file(self) -> Path:
        return self.extensions_dir / ".l0w-update.json"


def from_env() -> Settings:
    e = os.environ
    s = Settings(
        token=e.get("L0W_OPS_TOKEN", ""),
        docker_token=e.get("L0W_OPS_DOCKER_TOKEN", ""),
        extensions_dir=Path(e.get("OPS_EXTENSIONS_DIR", "/extensions")),
        toml_file=Path(e.get("OPS_TOML_FILE", "/project/extensions.toml")),
        channel=e.get("L0W_VERSION", "stable").strip() or "stable",
        profiles=e.get("L0W_COMPOSE_PROFILES", ""),
        docker_url=e.get("OPS_DOCKER_URL", "http://ops-docker:8796"),
        github_api=e.get("OPS_GITHUB_API", "https://api.github.com"),
        github_repo=e.get("OPS_GITHUB_REPO", "CyrusBrown/l0wagent"),
        home=Path(e.get("HOME", "/tmp")),
        allow_file_git=_flag("OPS_ALLOW_FILE_GIT"),
        allow_private_hosts=_flag("OPS_ALLOW_PRIVATE_HOSTS"),
    )
    return s
