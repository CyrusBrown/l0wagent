"""./l0w backup and restore: the command lines, without Docker."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "cli" / "l0w.py"


@pytest.fixture
def l0w(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("l0w_cli_backup", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "compose.yaml").write_text("")
    monkeypatch.setattr(mod, "need_docker", lambda: None)
    monkeypatch.setenv("L0W_BACKUP_PASSPHRASE", "pw")
    return mod


def record(l0w, monkeypatch, rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if kw.get("stdout") is not None and hasattr(kw["stdout"], "write"):
            kw["stdout"].write(b"ARCHIVE")
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(l0w.subprocess, "run", run)
    return calls


def test_backup_command_line(l0w, monkeypatch, tmp_path):
    calls = record(l0w, monkeypatch)
    out = tmp_path / "b.l0wbak"
    l0w.main(["backup", "-o", str(out)])
    cmd, kw = calls[0]
    assert cmd == ["docker", "compose", "run", "--rm", "-T", "-e", "L0W_BACKUP_PASSPHRASE", "backup", "create"]
    assert kw["env"]["L0W_BACKUP_PASSPHRASE"] == "pw"
    assert "L0W_AGENT_TOKEN" in kw["env"]  # placeholders: there's no .env yet
    assert out.read_bytes() == b"ARCHIVE" and oct(out.stat().st_mode & 0o777) == "0o600"


def test_backup_failure_removes_the_file(l0w, monkeypatch, tmp_path):
    record(l0w, monkeypatch, rc=1)
    out = tmp_path / "b.l0wbak"
    with pytest.raises(SystemExit):
        l0w.main(["backup", "-o", str(out)])
    assert not out.exists()


def test_restore_command_lines(l0w, monkeypatch, tmp_path):
    calls = record(l0w, monkeypatch)
    f = tmp_path / "b.l0wbak"
    f.write_bytes(b"x")
    l0w.main(["restore", str(f), "--yes"])
    cmds = [c for c, _ in calls]
    assert cmds[0] == ["docker", "compose", "stop"]
    assert cmds[1] == ["docker", "compose", "run", "--rm", "-T", "-e", "L0W_BACKUP_PASSPHRASE", "backup", "restore"]
    assert cmds[2] == ["docker", "compose", "up", "-d"]
    assert calls[1][1]["stdin"] is not None


def test_restore_failure_does_not_restart(l0w, monkeypatch, tmp_path):
    calls = record(l0w, monkeypatch, rc=1)
    f = tmp_path / "b.l0wbak"
    f.write_bytes(b"x")
    with pytest.raises(SystemExit):
        l0w.main(["restore", str(f), "--yes"])
    assert not any(c[:3] == ["docker", "compose", "up"] for c, _ in calls)


def test_existing_env_gets_no_placeholders(l0w, monkeypatch, tmp_path):
    calls = record(l0w, monkeypatch)
    Path(".env").write_text("L0W_AGENT_TOKEN=real\n")
    monkeypatch.delenv("L0W_AGENT_TOKEN", raising=False)
    l0w.main(["backup", "-o", str(tmp_path / "o")])
    assert "L0W_AGENT_TOKEN" not in calls[0][1]["env"]


def test_default_file_name(l0w, monkeypatch, tmp_path):
    record(l0w, monkeypatch)
    l0w.main(["backup"])
    assert list(tmp_path.glob("l0w-backup-*.l0wbak"))
