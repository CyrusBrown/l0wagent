"""./l0w update's pull step and checkout check (issue #2), without Docker."""

import importlib.util
import json
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
    monkeypatch.setattr(l0w, "add_ops_settings", lambda env: False)
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


# ── the ops containers (H6) ───────────────────────────────────────────────────


def test_no_drain_skips_the_wait_but_still_recreates_and_clears_the_hold(l0w, monkeypatch):
    calls = drain_calls(l0w, monkeypatch, lambda: pytest.fail("must not wait for idle"))
    setup_update(l0w, monkeypatch, calls)
    seen = []
    monkeypatch.setattr(l0w, "compose", lambda *a, **k: seen.append(a) or subprocess.CompletedProcess(a, 0, "1.0.0", ""))
    l0w.cmd_update(update_args(l0w, no_drain=True))
    assert ("up", "-d", "--remove-orphans") in seen
    assert [c[:2] for c in calls] == [("drain", "off")]  # no drain on, no idle polling; the hold is cleared at the end


def test_add_ops_settings_fills_an_older_env(l0w, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {"L0W_AGENT_TOKEN": "x"}
    assert l0w.add_ops_settings(env) is True
    a, b = env["L0W_OPS_TOKEN"], env["L0W_OPS_DOCKER_TOKEN"]
    assert len(a) >= 32 and len(b) >= 32 and a != b
    st = tmp_path.stat()
    assert (env["L0W_UID"], env["L0W_GID"]) == (str(st.st_uid), str(st.st_gid))
    before = dict(env)
    assert l0w.add_ops_settings(env) is False and env == before
    assert "COMPOSE_PROFILES" not in env  # updates from the web UI are the owner's choice


def test_updates_profile_toggles_and_keeps_other_profiles(l0w):
    env = {"COMPOSE_PROFILES": "tools"}
    l0w.set_updates(env, True)
    assert env["COMPOSE_PROFILES"] == "tools,ops-docker" and l0w.profiles(env) == ["tools", "ops-docker"]
    l0w.set_updates(env, True)
    assert env["COMPOSE_PROFILES"] == "tools,ops-docker"
    l0w.set_updates(env, False)
    assert env["COMPOSE_PROFILES"] == "tools"
    l0w.set_updates(env, False)
    env2 = {}
    l0w.set_updates(env2, True)
    l0w.set_updates(env2, False)
    assert "COMPOSE_PROFILES" not in env2


def test_ops_enable_and_disable(l0w, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    saved, ran = {}, []
    monkeypatch.setattr(l0w, "need_docker", lambda: None)
    monkeypatch.setattr(l0w, "env_file", lambda: dict(saved) or {"L0W_AGENT_TOKEN": "x", "COMPOSE_PROFILES": ""})
    monkeypatch.setattr(l0w, "write_env", lambda env: saved.clear() or saved.update(env))
    monkeypatch.setattr(l0w, "compose", lambda *a, **k: ran.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    import argparse
    l0w.cmd_ops(argparse.Namespace(action="enable-updates", yes=True))
    assert saved["COMPOSE_PROFILES"] == "ops-docker" and saved["L0W_OPS_DOCKER_TOKEN"]
    assert ("up", "-d", "--remove-orphans") in ran
    ran.clear()
    l0w.cmd_ops(argparse.Namespace(action="enable-updates", yes=True))
    assert ran == [] and "already on" in capsys.readouterr().out
    l0w.cmd_ops(argparse.Namespace(action="disable-updates", yes=True))
    assert "COMPOSE_PROFILES" not in saved
    assert ("--profile", "ops-docker", "rm", "-sf", "ops-docker") in ran and ran[-1] == ("up", "-d", "--remove-orphans")


def test_ops_enable_asks_and_defaults_to_no(l0w, monkeypatch):
    monkeypatch.setattr(l0w, "need_docker", lambda: None)
    monkeypatch.setattr(l0w, "env_file", lambda: {"L0W_AGENT_TOKEN": "x"})
    monkeypatch.setattr(l0w, "write_env", lambda env: pytest.fail("must not write"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    import argparse
    with pytest.raises(SystemExit):
        l0w.cmd_ops(argparse.Namespace(action="enable-updates", yes=False))


def test_install_files_exist_for_the_bind_mounts(l0w, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    l0w.ensure_install_files()
    assert (tmp_path / "extensions").is_dir() and (tmp_path / "extensions.toml").is_file()
    (tmp_path / "extensions.toml").write_text("# mine\n")
    l0w.ensure_install_files()
    assert (tmp_path / "extensions.toml").read_text() == "# mine\n"
    # Docker makes a folder when the file is missing at `up`: an empty one is put right
    (tmp_path / "extensions.toml").unlink()
    (tmp_path / "extensions.toml").mkdir()
    l0w.ensure_install_files()
    assert (tmp_path / "extensions.toml").is_file()
    (tmp_path / "extensions.toml").unlink()
    (tmp_path / "extensions.toml").mkdir()
    (tmp_path / "extensions.toml" / "x").write_text("1")
    with pytest.raises(SystemExit):
        l0w.ensure_install_files()


def test_wait_healthy_in_the_updater_asks_control_from_inside(l0w, monkeypatch):
    monkeypatch.setenv("L0W_IN_UPDATER", "1")
    seen = []
    monkeypatch.setattr(l0w, "compose", lambda *a, **k: seen.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(l0w._opener, "open", lambda *a, **k: pytest.fail("the host's port isn't this container's"))
    assert l0w.wait_healthy("8000") is True
    assert seen[0][:4] == ("exec", "-T", "control", "python")


EXT = "a" * 40


def sync_env(l0w, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "extensions").mkdir()
    monkeypatch.setattr(l0w, "call", lambda *a, **k: [])
    runs = []
    monkeypatch.setattr(l0w.subprocess, "run", lambda cmd, **k: runs.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    return runs


def test_sync_reads_both_toml_files_and_the_owners_wins(l0w, tmp_path, monkeypatch, capsys):
    runs = sync_env(l0w, tmp_path, monkeypatch)
    (tmp_path / "extensions.toml").write_text(f'[extension.alpha]\ngit = "https://x/mine.git"\ncommit = "{EXT}"\n')
    (tmp_path / "extensions" / ".ops.toml").write_text(f'[extension.alpha]\ngit = "https://x/ui.git"\ncommit = "{EXT}"\n[extension.beta]\ngit = "https://x/b.git"\ncommit = "{EXT}"\n')
    l0w.cmd_extensions_sync()
    clones = {c[-2]: c[-1] for c in runs if c[:3] == ["git", "clone", "--quiet"]}
    assert set(clones) == {"https://x/mine.git", "https://x/b.git"}
    assert "alpha: https://x/mine.git" in capsys.readouterr().out


def test_sync_leaves_what_the_web_ui_installed_alone(l0w, tmp_path, monkeypatch, capsys):
    runs = sync_env(l0w, tmp_path, monkeypatch)
    (tmp_path / "extensions" / ".ops.toml").write_text(f'[extension.alpha]\ngit = "https://x/a.git"\ncommit = "{EXT}"\n[extension.beta]\ngit = "https://x/b.git"\ncommit = "{EXT}"\n')
    for name, commit in (("alpha", EXT), ("beta", "b" * 40)):
        (tmp_path / "extensions" / name).mkdir()
        (tmp_path / "extensions" / name / ".l0w-source.json").write_text(json.dumps({"commit": commit}))
    with pytest.raises(SystemExit):  # beta is at another commit than the file says: reported, not touched
        l0w.cmd_extensions_sync()
    cap = capsys.readouterr()
    assert "alpha: https://x/a.git @ aaaaaaaaaaaa (installed from the web UI)" in cap.out and "beta: installed from the web UI" in cap.err
    assert not [c for c in runs if c[0] == "git"]


def test_sync_skips_entries_with_a_path(l0w, tmp_path, monkeypatch, capsys):
    runs = sync_env(l0w, tmp_path, monkeypatch)
    (tmp_path / "extensions" / ".ops.toml").write_text(f'[extension.alpha]\ngit = "https://x/a.git"\ncommit = "{EXT}"\npath = "ext/alpha"\n')
    with pytest.raises(SystemExit):
        l0w.cmd_extensions_sync()
    assert "has a path" in capsys.readouterr().err and not runs


def test_backup_placeholders_cover_the_compose_files_required_variables(l0w):
    import re
    from pathlib import Path
    text = (Path(l0w.__file__).resolve().parents[1] / "compose.yaml").read_text()
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", text))
    assert required <= set(l0w.BACKUP_PLACEHOLDERS), required - set(l0w.BACKUP_PLACEHOLDERS)
