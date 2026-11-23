import json
import os
import tempfile
import time

import httpx
import pytest
from conftest import H, post
from fakedocker import PROJECT, SELF, FakeDocker
from starlette.testclient import TestClient

from l0w_ops import docker as dockermod
from l0w_ops.app import create_app


@pytest.fixture
def fake():
    d = tempfile.mkdtemp(prefix="fd", dir="/tmp")
    f = FakeDocker(os.path.join(d, "d.sock"))
    f.start()
    yield f
    f.stop()


def github(real: set[str], calls: list | None = None, status: int | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if status:
            return httpx.Response(status)
        tag = str(request.url).rsplit("/", 1)[1]
        return httpx.Response(200 if tag in real else 404, json={})
    return httpx.MockTransport(handle)


def wait_phase(client, phase, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        p = client.get("/v0/status", headers=H).json()["update_progress"]
        if p and p["phase"] == phase:
            return p
        time.sleep(0.05)
    raise AssertionError(client.get("/v0/status", headers=H).json())


@pytest.fixture
def wired(settings, fake):
    """ops with ops-docker switched on, talking to a real ops-docker app over ASGI, which talks to the fake Docker socket."""
    settings.profiles = "tools,ops-docker"
    calls = []
    docker_app = dockermod.create_app(settings.docker_token, api=dockermod.DockerAPI(fake.path), self_id=SELF)
    app = create_app(settings, docker_transport=httpx.ASGITransport(app=docker_app, client=("1.2.3.4", 1)), github_transport=github({"v0.9.1"}, calls))
    with TestClient(app) as c:
        yield c, calls, fake


def test_update_is_refused_without_ops_docker(client):
    r = post(client, "/v0/update", {"version": "0.9.1"})
    assert r.status_code == 409 and r.json()["error"] == "updates_off"
    assert client.get("/v0/status", headers=H).json()["docker"] is False


def test_update_pulls_then_starts_the_updater(wired):
    client, calls, fake = wired
    assert client.get("/v0/status", headers=H).json()["docker"] is True
    r = post(client, "/v0/update", {"version": "0.9.1"})
    assert r.status_code == 200 and r.json() == {"started": True}
    wait_phase(client, "updater_started")
    assert calls == ["https://api.github.com/repos/CyrusBrown/l0wagent/releases/tags/v0.9.1"]
    posts = [q[1] for q in fake.requests if q[0] == "POST"]
    assert posts[:7] == ["/images/create"] * 7 and posts[7:] == ["/containers/create", f"/containers/{'f' * 64}/start"]
    create = next(q for q in fake.requests if q[1] == "/containers/create")
    assert create[3]["Cmd"] == ["l0w-updater", "0.9.1"] and create[2] == {"name": f"{PROJECT}-updater"}


def test_update_refuses_a_fake_version(wired):
    client, calls, fake = wired
    r = post(client, "/v0/update", {"version": "9.9.9"})
    assert r.status_code == 422 and r.json()["error"] == "not_a_release"
    assert fake.requests == [q for q in fake.requests if q[0] == "GET"]  # status probe only, nothing pulled or created
    assert not [q for q in fake.requests if q[0] == "POST"]
    for bad in ("stable", "../x", "0.9.1;x", "", None, "latest"):
        assert post(client, "/v0/update", {"version": bad}).status_code == 400
    assert calls == ["https://api.github.com/repos/CyrusBrown/l0wagent/releases/tags/v9.9.9"]


def test_edge_only_on_the_edge_channel(wired, settings):
    client, calls, fake = wired
    assert post(client, "/v0/update", {"version": "edge"}).status_code == 422
    settings.channel = "edge"
    assert post(client, "/v0/update", {"version": "edge"}).status_code == 200
    wait_phase(client, "updater_started")
    assert calls == []  # edge isn't a release: no lookup either way


def test_release_lookup_errors_are_not_a_yes(settings, fake):
    settings.profiles = "ops-docker"
    app = create_app(settings, github_transport=github(set(), status=503))
    with TestClient(app) as c:
        r = post(c, "/v0/update", {"version": "0.9.1"})
        assert r.status_code == 502 and r.json()["error"] == "release_check_failed"

    def boom(request):
        raise httpx.ConnectError("no network")
    with TestClient(create_app(settings, github_transport=httpx.MockTransport(boom))) as c:
        assert post(c, "/v0/update", {"version": "0.9.1"}).status_code == 502


def test_a_failing_docker_side_shows_in_status(settings, fake):
    settings.profiles = "ops-docker"
    fake.pull_errors["ghcr.io/cyrusbrown/l0wagent-ops"] = "denied"
    docker_app = dockermod.create_app(settings.docker_token, api=dockermod.DockerAPI(fake.path), self_id=SELF)
    app = create_app(settings, docker_transport=httpx.ASGITransport(app=docker_app), github_transport=github({"v0.9.1"}))
    with TestClient(app) as c:
        assert post(c, "/v0/update", {"version": "0.9.1"}).status_code == 200
        p = wait_phase(c, "failed")
        assert "denied" in p["error"]
        assert not [q for q in fake.requests if q[1] == "/containers/create"]


def test_unreachable_ops_docker(settings):
    settings.profiles = "ops-docker"

    def refuse(request):
        raise httpx.ConnectError("down")
    app = create_app(settings, docker_transport=httpx.MockTransport(refuse), github_transport=github({"v0.9.1"}))
    with TestClient(app) as c:
        assert c.get("/v0/status", headers=H).json()["docker"] is False and c.get("/v0/status", headers=H).json()["docker_enabled"] is True
        post(c, "/v0/update", {"version": "0.9.1"})
        assert "isn't reachable" in wait_phase(c, "failed")["error"]


def test_status_reports_the_updaters_result_file(wired, settings):
    client, *_ = wired
    settings.update_file.write_text(json.dumps({"tag": "0.9.1", "ok": True, "before": "0.9.0", "after": "0.9.1"}))
    assert client.get("/v0/status", headers=H).json()["update"]["after"] == "0.9.1"
