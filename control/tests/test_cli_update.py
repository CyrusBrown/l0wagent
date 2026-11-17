"""./l0w update's pull step and checkout check (issue #2), without Docker."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "cli" / "l0w.py"


@pytest.fixture
def l0w():
    spec = importlib.util.spec_from_file_location("l0w_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_pull(l0w, monkeypatch, failing: set[str], local: set[str]):
    images = {s: f"ghcr.io/x/{s}:stable" for s in ("control", "agent", "chat-runtime")}
    monkeypatch.setattr(l0w, "service_images", lambda: images)
    monkeypatch.setattr(l0w, "have_image", lambda image: image in local)

    def compose(*args, check=True, capture=False):
        rc = 18 if args[-1] in failing else 0
        return subprocess.CompletedProcess(args, rc, "", "Error response from daemon: manifest unknown" if rc else "")

    monkeypatch.setattr(l0w, "compose", compose)
    return images


def test_pull_keeps_a_local_image_the_registry_lacks(l0w, monkeypatch, capsys):
    images = fake_pull(l0w, monkeypatch, failing={"chat-runtime"}, local={"ghcr.io/x/chat-runtime:stable"})
    l0w.pull_images()
    out = capsys.readouterr().out
    assert "control: pulled" in out
    assert f"chat-runtime: couldn't pull {images['chat-runtime']} (Error response from daemon: manifest unknown); keeping" in out


def test_pull_stops_when_an_image_is_nowhere(l0w, monkeypatch, capsys):
    fake_pull(l0w, monkeypatch, failing={"chat-runtime", "agent"}, local={"ghcr.io/x/agent:stable"})
    with pytest.raises(SystemExit):
        l0w.pull_images()
    err = capsys.readouterr().err
    assert "ghcr.io/x/chat-runtime:stable" in err and "ghcr.io/x/agent:stable" not in err


def repo(tmp_path: Path) -> Path:
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    g("tag", "v0.5.0")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
    return tmp_path


def test_checkout_ahead_of_the_release_is_flagged(l0w, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo(tmp_path))
    l0w.check_checkout("stable")
    out = capsys.readouterr().out
    assert "the stable images are v0.5.0" in out and "git checkout v0.5.0" in out


def test_checkout_at_the_release_or_on_edge_is_quiet(l0w, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo(tmp_path))
    l0w.check_checkout("edge")
    subprocess.run(["git", "checkout", "-q", "v0.5.0"], cwd=tmp_path, check=True)
    l0w.check_checkout("stable")
    l0w.check_checkout("0.5.0")
    l0w.check_checkout("9.9.9")  # no such tag: nothing to compare with
    assert capsys.readouterr().out == ""
