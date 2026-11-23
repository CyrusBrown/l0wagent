"""The hub's signed service document names the test extension (dev/hub/echo-extension); ops installs it under extensions/echo
at the pinned commit, and a newer commit the hub publishes replaces it."""

import json
import tomllib
from pathlib import Path

from conftest import HUB, Repo, post, service_doc

from l0w_ops.extensions import SOURCE_FILE

SRC = Path(__file__).resolve().parents[2] / "dev" / "hub" / "echo-extension"
HTTPS = "https://git.example/echo-extension.git"


def files() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text() for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def body(sha, ref_commit=None):
    ref = {"git": HTTPS, "commit": ref_commit or sha, "path": "."}
    return {"name": "echo", "git": HTTPS, "commit": sha, "source": "hub", "service_doc": service_doc(ref), "hub_key": HUB.key.to_dict()}


def test_the_hub_installs_and_updates_the_echo_extension(client, settings, tmp_path):
    repo = Repo(tmp_path / "echo-remote")
    first = repo.commit(files(), "first")
    settings.git_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{repo.path}.insteadOf", "GIT_CONFIG_VALUE_0": HTTPS}
    r = post(client, "/v0/extensions/install", body(first))
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is False
    d = settings.extensions_dir / "echo"
    assert tomllib.loads((d / "extension.toml").read_text())["name"] == "echo" and (d / "extension.py").is_file()
    src = json.loads((d / SOURCE_FILE).read_text())
    assert src["source"] == "hub" and src["commit"] == first and src["git"] == HTTPS
    assert not settings.ops_toml.exists()  # a hub's list isn't the owner's pinned list

    # a document that names another commit does not install this one
    assert post(client, "/v0/extensions/install", body(first, ref_commit="f" * 40)).status_code >= 400

    # the hub publishes a newer version
    newer = {**files(), "extension.toml": files()["extension.toml"].replace('version = "0.1.0"', 'version = "0.2.0"')}
    second = repo.commit(newer, "second")
    r = post(client, "/v0/extensions/install", body(second))
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is True
    assert 'version = "0.2.0"' in (d / "extension.toml").read_text()
    assert json.loads((d / SOURCE_FILE).read_text())["commit"] == second
