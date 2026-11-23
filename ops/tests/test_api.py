import pytest
from conftest import TOKEN, H, post
from starlette.testclient import TestClient

from l0w_ops.app import create_app
from l0w_ops.config import Settings

ENDPOINTS = [("GET", "/v0/status"), ("POST", "/v0/extensions/resolve"), ("POST", "/v0/extensions/install"), ("POST", "/v0/extensions/remove"), ("POST", "/v0/update")]


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_every_command_needs_the_token(client, method, path):
    for h in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": "Bearer "}, {"Authorization": f"Bearer {TOKEN}x"}):
        r = client.request(method, path, json={}, headers=h)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", (path, h)


def test_unknown_paths_and_methods(client):
    assert client.get("/v0/extensions/install", headers=H).status_code == 405
    r = client.post("/v0/exec", json={}, headers=H)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert client.get("/", headers=H).status_code == 404


def test_refuses_to_start_without_a_token(settings):
    settings.token = ""
    with pytest.raises(ValueError):
        create_app(settings)
    with pytest.raises(ValueError):
        create_app(Settings(token=""))
    from l0w_ops.docker import create_app as docker_app
    with pytest.raises(ValueError):
        docker_app("")


def test_bodies_must_be_json_objects(client):
    for raw in (b"[1]", b"not json", b'"x"'):
        r = client.post("/v0/extensions/install", content=raw, headers=H)
        assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert client.post("/v0/extensions/install", content=b"x" * 300_000, headers=H).status_code == 413


def test_status_with_nothing_installed(client, settings):
    st = client.get("/v0/status", headers=H).json()
    assert st == {"docker": False, "docker_enabled": False, "channel": "stable", "extensions": [], "update": None, "update_progress": None}
    settings.update_file.write_text('{"tag": "0.9.1", "ok": true}')
    assert client.get("/v0/status", headers=H).json()["update"] == {"tag": "0.9.1", "ok": True}
    settings.update_file.write_text("garbage")
    assert client.get("/v0/status", headers=H).json()["update"] is None


def test_status_lists_only_what_ops_installed(client, settings, repo):
    (settings.extensions_dir / "mine").mkdir()
    (settings.extensions_dir / ".cache").mkdir()
    post(client, "/v0/extensions/install", {"name": "plantlog", "git": repo.url, "commit": repo.sha, "source": "owner"})
    names = [e["name"] for e in client.get("/v0/status", headers=H).json()["extensions"]]
    assert names == ["plantlog"]


def test_install_lock_serializes(settings):
    import threading

    from l0w_ops import extensions

    gate, order = threading.Event(), []
    real = extensions.install

    def slow(s, body):
        order.append("start-" + body["name"])
        if body["name"] == "aa":
            gate.wait(2)
        order.append("end-" + body["name"])
        return {}

    extensions.install = slow
    try:
        with TestClient(create_app(settings)) as c:
            t = threading.Thread(target=lambda: post(c, "/v0/extensions/install", {"name": "aa"}))
            t.start()
            import time
            time.sleep(0.3)
            threading.Timer(0.5, gate.set).start()
            post(c, "/v0/extensions/install", {"name": "bb"})
            t.join()
    finally:
        extensions.install = real
    assert order == ["start-aa", "end-aa", "start-bb", "end-bb"]
