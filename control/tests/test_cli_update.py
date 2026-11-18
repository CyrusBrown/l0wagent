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


def drain_calls(l0w, monkeypatch, idle):
    calls = []

    def hostctl(*args):
        calls.append(args)
        if args[0] == "idle":
            return idle()
        return {}

    monkeypatch.setattr(l0w, "hostctl", hostctl)
    return calls


def update_args(l0w, **kw):
    import argparse
    return argparse.Namespace(timeout=1, force=False, version=None, cancel=False, **kw)


def setup_update(l0w, monkeypatch, calls):
    monkeypatch.setattr(l0w, "need_docker", lambda: None)
    monkeypatch.setattr(l0w, "env_file", lambda: {"L0W_PORT": "8000"})
    monkeypatch.setattr(l0w, "add_chat_settings", lambda env: False)
    monkeypatch.setattr(l0w, "add_hub_settings", lambda env: False)
    monkeypatch.setattr(l0w, "check_checkout", lambda v: None)
    monkeypatch.setattr(l0w, "pull_images", lambda: None)
    monkeypatch.setattr(l0w, "wait_healthy", lambda port: True)
    monkeypatch.setattr(l0w.time, "sleep", lambda s: None)
    monkeypatch.setattr(l0w.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(l0w, "compose", lambda *a, **k: subprocess.CompletedProcess(a, 0, "1.0.0", ""))


def test_update_clears_the_drain_when_it_dies(l0w, monkeypatch):
    calls = drain_calls(l0w, monkeypatch, lambda: {"idle": True, "waiting_for": []})
    setup_update(l0w, monkeypatch, calls)

    def boom(*a, **k):
        raise RuntimeError("compose exploded")

    monkeypatch.setattr(l0w, "compose", lambda *a, **k: boom() if a[0] == "up" else subprocess.CompletedProcess(a, 0, "1.0.0", ""))
    with pytest.raises(RuntimeError):
        l0w.cmd_update(update_args(l0w))
    assert calls[0][:2] == ("drain", "on") and calls[-1] == ("drain", "off")


def test_update_clears_the_drain_on_sigterm(l0w, monkeypatch):
    import os
    import signal

    def idle():
        os.kill(os.getpid(), signal.SIGTERM)
        return {"idle": False, "waiting_for": ["run #1 (chat) is in progress"]}

    calls = drain_calls(l0w, monkeypatch, idle)
    setup_update(l0w, monkeypatch, calls)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        l0w.cmd_update(update_args(l0w))
    assert calls[-1] == ("drain", "off")
    assert signal.getsignal(signal.SIGTERM) is before


def test_update_survives_control_being_down_when_clearing(l0w, monkeypatch, capsys):
    def hostctl(*args):
        if args[:2] == ("drain", "off"):
            raise RuntimeError("control is down")
        return {"idle": True, "waiting_for": []}

    monkeypatch.setattr(l0w, "hostctl", hostctl)
    setup_update(l0w, monkeypatch, [])
    l0w.cmd_update(update_args(l0w))
    assert "./l0w drain off" in capsys.readouterr().err


def test_drain_off_command(l0w, monkeypatch):
    calls = drain_calls(l0w, monkeypatch, lambda: {})
    monkeypatch.setattr(l0w, "need_docker", lambda: None)
    l0w.cmd_drain(None)
    assert calls == [("drain", "off")]


def test_update_removes_orphaned_containers(l0w, monkeypatch):
    """Services dropped from the compose file (agent-web) are removed on existing installs."""
    calls = drain_calls(l0w, monkeypatch, lambda: {"idle": True, "waiting_for": []})
    setup_update(l0w, monkeypatch, calls)
    seen = []
    monkeypatch.setattr(l0w, "compose", lambda *a, **k: seen.append(a) or subprocess.CompletedProcess(a, 0, "1.0.0", ""))
    l0w.cmd_update(update_args(l0w))
    assert ("up", "-d", "--remove-orphans") in seen


def test_add_hub_settings_fills_a_pre_hub_env(l0w, monkeypatch):
    taken = []
    monkeypatch.setattr(l0w, "pick_subnet", lambda name, t=(): taken.append(t) or "172.31.220.0/24")
    env = {"SANDBOX_SUBNET": "172.31.240.0/24", "CHAT_SUBNET": "172.31.230.0/24"}
    assert l0w.add_hub_settings(env) is True
    assert env["TUNNEL_SUBNET"] == "172.31.220.0/24"
    assert taken == [("172.31.240.0/24", "172.31.230.0/24")]
    a, b = env["L0W_GATEWAY_TOKEN"], env["L0W_GATEWAY_TUNNEL_TOKEN"]
    assert len(a) >= 32 and len(b) >= 32 and a != b


def test_add_hub_settings_keeps_what_is_there(l0w, monkeypatch):
    monkeypatch.setattr(l0w, "pick_subnet", lambda *a, **k: pytest.fail("must not pick again"))
    env = {"L0W_GATEWAY_TOKEN": "a", "L0W_GATEWAY_TUNNEL_TOKEN": "b", "TUNNEL_SUBNET": "10.231.3.0/24"}
    before = dict(env)
    assert l0w.add_hub_settings(env) is False
    assert env == before
