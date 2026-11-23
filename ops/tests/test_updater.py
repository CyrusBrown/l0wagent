import json
from pathlib import Path

import pytest

from l0w_ops import updater

FAKE_CLI = """\
import os, sys
print("args:", sys.argv[1:], "in updater:", os.environ.get("L0W_IN_UPDATER"))
print("Updated: 0.9.0 \\u2192 0.9.1.")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "l0w.py").write_text(FAKE_CLI)
    (tmp_path / "compose.yaml").write_text("name: x\n")
    (tmp_path / "extensions").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def result(project: Path) -> dict:
    return json.loads((project / "extensions" / ".l0w-update.json").read_text())


def test_runs_the_cli_update_without_drain_and_records_the_result(project, capsys):
    assert updater.main(["0.9.1"]) == 0
    r = result(project)
    assert r["tag"] == "0.9.1" and r["ok"] is True and r["before"] == "0.9.0" and r["after"] == "0.9.1" and r["started_at"] and r["finished_at"]
    assert "['update', '--version', '0.9.1', '--no-drain']" in r["tail"] and "in updater: 1" in r["tail"]
    assert not [p for p in (project / "extensions").iterdir() if p.name.startswith(".tmp-")]
    assert "Updated" in capsys.readouterr().out


def test_a_failed_update_is_recorded(project, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "3")
    assert updater.main(["edge"]) == 1
    r = result(project)
    assert r["ok"] is False and r["tag"] == "edge" and r["finished_at"]


def test_not_a_project_folder(tmp_path, monkeypatch):
    (tmp_path / "extensions").mkdir()
    monkeypatch.chdir(tmp_path)
    assert updater.main(["0.9.1"]) == 1
    assert "isn't an l0wagent folder" in json.loads((tmp_path / "extensions" / ".l0w-update.json").read_text())["tail"]


@pytest.mark.parametrize("args", [[], ["a", "b"], ["latest"], ["0.9.1;rm"], ["--help"]])
def test_usage(project, args):
    assert updater.main(args) == 2
    assert not (project / "extensions" / ".l0w-update.json").exists()


def test_the_console_script_is_declared():
    import tomllib

    pp = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert pp["project"]["scripts"]["l0w-updater"] == "l0w_ops.updater:main"
