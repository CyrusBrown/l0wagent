"""The Claude login check: the usage reader notices when Anthropic turns the agent's login down,
the subscription's status says so, and a good reading (after signing in again) clears it."""
import asyncio
import json
import time

import httpx

from l0w_control.backends.sqlite import SqliteBackend
from l0w_control.config import Env
from l0w_control.runs import usage as usage_mod

REAL_CLIENT = httpx.AsyncClient


def make(tmp_path, monkeypatch, expires_in: float = 3600):
    inbox = tmp_path / "work" / "_inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("L0W_INBOX_DIR", str(inbox))
    home = tmp_path / "agent-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "oauth-abc", "expiresAt": int((time.time() + expires_in) * 1000)}}))
    env = Env(data_dir=tmp_path / "data", agent_token="agent-secret", sandbox_subnet="10.255.255.0/24", runner_url="http://127.0.0.1:9",
              egress_url="http://127.0.0.1:9", agent_home=home, usage_url="https://usage.test/api/oauth/usage")
    return SqliteBackend(env=env), home


def answer(monkeypatch, status: int):
    def handler(req: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json={"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}})
    monkeypatch.setattr(usage_mod.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_a_rejected_login_is_reported_and_cleared_by_a_good_reading(tmp_path, monkeypatch):
    be, _ = make(tmp_path, monkeypatch)
    answer(monkeypatch, 401)
    asyncio.run(be.usage.read(max_age=0))
    assert be.usage.login_problem
    assert be._provider_info("claude-subscription").status.startswith("Signed out:")

    answer(monkeypatch, 200)
    asyncio.run(be.usage.read(max_age=0))
    assert be.usage.login_problem is None
    assert be._provider_info("claude-subscription").status.startswith("Weekly usage 20%")


def test_an_expired_token_is_not_a_problem_until_a_refresh_fails_to_renew_it(tmp_path, monkeypatch):
    be, _ = make(tmp_path, monkeypatch, expires_in=-60)
    answer(monkeypatch, 200)
    asyncio.run(be.usage.read(max_age=0))  # no refresh offered: the CLI renews it on its next run
    assert be.usage.login_problem is None

    async def refresh_that_renews_nothing():
        return None
    asyncio.run(be.usage.read(max_age=0, refresh=refresh_that_renews_nothing))
    assert be.usage.login_problem

    async def refresh_that_cannot_run():
        raise RuntimeError("a run is in progress")
    be.usage.login_problem = None
    asyncio.run(be.usage.read(max_age=0, refresh=refresh_that_cannot_run))
    assert be.usage.login_problem is None  # says nothing about the login


def test_a_network_error_keeps_what_was_known(tmp_path, monkeypatch):
    be, _ = make(tmp_path, monkeypatch)
    answer(monkeypatch, 401)
    asyncio.run(be.usage.read(max_age=0))
    answer(monkeypatch, 503)
    asyncio.run(be.usage.read(max_age=0))
    assert be.usage.login_problem


def test_a_login_file_without_a_token_is_a_problem_and_no_file_is_not(tmp_path, monkeypatch):
    be, home = make(tmp_path, monkeypatch)
    creds = home / ".claude" / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    asyncio.run(be.usage.read(max_age=0))
    assert be.usage.login_problem
    creds.unlink()
    asyncio.run(be.usage.read(max_age=0))
    assert be.usage.login_problem is None  # not logged in at all: the runner says so, and Add provider offers the sign-in
