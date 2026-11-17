"""The runner's S23/S24 pieces that don't need Docker: who may call it, spotting a second model
session, and when consults run as the reviewer's unix user."""

import importlib.util
from pathlib import Path

import pytest

from l0w_control.runs.runtime import ClaudeCode

RUNNER_PY = Path(__file__).resolve().parents[2] / "agent" / "runner" / "runner.py"


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("runner_under_test", RUNNER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_accepts_only_allowed_addresses(runner, monkeypatch):
    assert runner.source_allowed("1.2.3.4")  # unset: anyone (dev_stack)
    monkeypatch.setattr(runner, "ALLOWED_FROM", ["172.31.240.0/24", "10.0.0.5"])
    assert runner.source_allowed("172.31.240.7")
    assert runner.source_allowed("::ffff:10.0.0.5")
    assert not runner.source_allowed("172.31.241.7")
    assert not runner.source_allowed("127.0.0.1")
    assert not runner.source_allowed(None)


def test_runner_resolves_host_names(runner, monkeypatch):
    monkeypatch.setattr(runner, "ALLOWED_FROM", ["localhost"])
    monkeypatch.setattr(runner, "_resolved", {})
    assert runner.source_allowed("127.0.0.1")
    assert not runner.source_allowed("10.9.9.9")


def test_foreign_model_cli_is_spotted(runner, monkeypatch):
    # runner(1) -> claude(10, the session) -> bash(11) -> claude -p(12): a second session
    #           -> node(20) -> opencode(21): the session's own launcher chain (opencode runtime, root 20)
    #  cron(30) -> claude -p(31): not in the session's tree at all; `claude --version` isn't a session
    table = {
        10: (1, ["claude", "-p"]), 11: (10, ["bash", "-c", "claude -p hi"]), 12: (11, ["claude", "-p", "hi"]),
        20: (1, ["node", "/usr/lib/opencode"]), 21: (20, ["/usr/local/bin/opencode", "run"]),
        30: (1, ["cron"]), 31: (30, ["claude", "-p", "later"]), 32: (1, ["claude", "--version"]),
    }
    monkeypatch.setattr(runner, "_proc_table", lambda: table)
    assert {p["pid"] for p in runner.foreign_cli({10})} == {12, 31, 20, 21}
    assert {p["pid"] for p in runner.foreign_cli({10, 20})} == {12, 31}


def test_reviewer_falls_back_without_the_account(runner, monkeypatch):
    monkeypatch.setattr(runner, "REVIEWER_USER", "no-such-user-here")
    monkeypatch.setattr(runner, "_reviewer_logged", False)
    assert runner.reviewer_account() is None
    assert runner._reviewer_logged


def test_reviewer_env_has_no_secrets(runner, monkeypatch):
    for k in ("RUNNER_TOKEN", "AGENT_API_TOKEN", "CONTROL_URL", "SOME_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    pw = runner.pwd.getpwuid(0)
    env = runner._reviewer_env(pw, {"OPENCODE_API_KEY": "k", "AGENT_API_TOKEN": "nope"})
    assert env["PATH"] == "/usr/bin" and env["OPENCODE_API_KEY"] == "k" and env["HOME"] == pw.pw_dir
    assert not {"RUNNER_TOKEN", "AGENT_API_TOKEN", "CONTROL_URL", "SOME_SECRET"} & set(env)


def test_run_timeline_warns_about_a_second_session():
    p = ClaudeCode().parse({"type": "runner", "event": "foreign_cli", "processes": [{"pid": 12, "cmd": "claude -p hi"}]})
    assert p.items and "claude -p hi" in p.items[0].text and p.items[0].kind == "control"
