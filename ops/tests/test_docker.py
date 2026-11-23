import os
import tempfile

import pytest
from fakedocker import NEW_ID, OLD_ID, PROJECT, SELF, WORKDIR, FakeDocker
from starlette.testclient import TestClient

from l0w_ops.docker import IMAGES, OPS_IMAGE, DockerAPI, create_app

T = "docker-token"
H = {"Authorization": f"Bearer {T}"}


@pytest.fixture
def fake():
    d = tempfile.mkdtemp(prefix="fd", dir="/tmp")  # a short path: unix socket names are limited to ~100 bytes
    f = FakeDocker(os.path.join(d, "d.sock"))
    f.start()
    yield f
    f.stop()


@pytest.fixture
def client(fake):
    with TestClient(create_app(T, api=DockerAPI(fake.path), self_id=SELF)) as c:
        yield c


def test_images_are_this_projects_fixed_list():
    assert IMAGES == [f"ghcr.io/cyrusbrown/l0wagent-{n}" for n in ("control", "agent", "egress", "chat-runtime", "gateway", "tunnel", "ops")]


def test_status_lists_this_projects_containers(client, fake):
    r = client.get("/v0/status", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == PROJECT and body["containers"][0]["service"] == "control" and body["containers"][0]["version"] == "0.9.0"
    assert fake.requests == [
        ("GET", f"/containers/{SELF}/json", {}, None),
        ("GET", "/containers/json", {"all": "1", "filters": '{"label": ["com.docker.compose.project=l0wagent"]}'}, None),
    ]


def test_pull_asks_for_exactly_the_projects_images(client, fake):
    r = client.post("/v0/pull", json={"tag": "0.9.1"}, headers=H)
    assert r.status_code == 200 and r.json() == {"pulled": IMAGES, "failed": {}}
    assert fake.requests == [("POST", "/images/create", {"fromImage": i, "tag": "0.9.1"}, None) for i in IMAGES]


def test_pull_failure_in_the_stream_is_reported(client, fake):
    fake.pull_errors["ghcr.io/cyrusbrown/l0wagent-tunnel"] = "manifest unknown"
    r = client.post("/v0/pull", json={"tag": "edge"}, headers=H)
    assert r.status_code == 200 and r.json()["failed"] == {"ghcr.io/cyrusbrown/l0wagent-tunnel": "manifest unknown"}
    fake.pull_errors[OPS_IMAGE] = "denied"  # the updater's own image must come
    r = client.post("/v0/pull", json={"tag": "edge"}, headers=H)
    assert r.status_code == 502 and r.json()["error"] == "pull_failed"


EXPECTED_BODY = {
    "Image": "ghcr.io/cyrusbrown/l0wagent-ops:0.9.1",
    "Cmd": ["l0w-updater", "0.9.1"],
    "User": "0:0",
    "WorkingDir": WORKDIR,
    "Env": ["L0W_IN_UPDATER=1"],
    "Labels": {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "l0w-updater", "com.docker.compose.oneoff": "True", "l0w.updater": "1"},
    "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock", f"{WORKDIR}:{WORKDIR}"], "AutoRemove": False, "Privileged": False,
                   "RestartPolicy": {"Name": "no"}, "SecurityOpt": ["no-new-privileges:true"]},
}


def test_run_updater_builds_exactly_the_designed_container(client, fake):
    r = client.post("/v0/run-updater", json={"tag": "0.9.1"}, headers=H)
    assert r.status_code == 200 and r.json()["started"] is True
    assert fake.requests == [
        ("GET", f"/containers/{SELF}/json", {}, None),
        ("GET", "/containers/json", {"all": "1", "filters": '{"label": ["com.docker.compose.project=l0wagent", "l0w.updater=1"]}'}, None),
        ("POST", "/containers/create", {"name": "l0wagent-updater"}, EXPECTED_BODY),
        ("POST", f"/containers/{NEW_ID}/start", {}, None),
    ]


def test_run_updater_removes_a_finished_one_and_refuses_while_one_runs(client, fake):
    fake.updaters = [{"Id": OLD_ID, "State": "exited"}]
    assert client.post("/v0/run-updater", json={"tag": "edge"}, headers=H).status_code == 200
    assert ("DELETE", f"/containers/{OLD_ID}", {"v": "1"}, None) in fake.requests
    fake.requests.clear()
    fake.updaters = [{"Id": OLD_ID, "State": "running"}]
    r = client.post("/v0/run-updater", json={"tag": "edge"}, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "update_running"
    assert not [q for q in fake.requests if q[0] != "GET"]


def test_docker_errors_are_passed_on(client, fake):
    fake.create_status = 404
    r = client.post("/v0/run-updater", json={"tag": "0.9.1"}, headers=H)
    assert r.status_code == 502 and "No such image" in r.json()["detail"]


@pytest.mark.parametrize("tag", ["", "stable", "latest", "0.9", "v0.9.1", "0.9.1;rm", "0.9.1 x", "../x", "edge2", "-1.0.0", "1.0.0/../x", "0.9.1\n", None, 5, "1.0.0-" + "a" * 100])
def test_tag_rules(client, fake, tag):
    for path in ("/v0/pull", "/v0/run-updater"):
        r = client.post(path, json={"tag": tag}, headers=H)
        assert r.status_code == 400, (path, tag)
    assert fake.requests == []


@pytest.mark.parametrize("tag", ["edge", "0.9.1", "1.0.0-rc1", "0.9.1-edge.abc1234", "10.20.30"])
def test_good_tags(client, tag):
    assert client.post("/v0/run-updater", json={"tag": tag}, headers=H).status_code == 200


def test_anything_else_is_404_and_reaches_docker_never(client, fake):
    for method, path in [("GET", "/containers/json"), ("POST", "/containers/create"), ("GET", "/v0/containers"), ("POST", "/v0/exec"), ("DELETE", "/v0/pull"), ("GET", "/images/json"),
                         ("POST", "/v0/run"), ("GET", "/version"), ("POST", "/containers/abc/kill"), ("GET", "/v0/status/../../containers/json")]:
        r = client.request(method, path, json={}, headers=H)
        assert r.status_code in (404, 405), (method, path)
    assert fake.requests == []


@pytest.mark.parametrize(("method", "path"), [("GET", "/v0/status"), ("POST", "/v0/pull"), ("POST", "/v0/run-updater")])
def test_token_required(client, fake, method, path):
    for h in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer docker-token-x"}):
        assert client.request(method, path, json={"tag": "edge"}, headers=h).status_code == 401
    assert fake.requests == []


@pytest.mark.parametrize("labels", [{}, {"com.docker.compose.project": "Bad Name", "com.docker.compose.project.working_dir": "/x"},
                                    {"com.docker.compose.project": "ok", "com.docker.compose.project.working_dir": "relative/path"},
                                    {"com.docker.compose.project": "ok", "com.docker.compose.project.working_dir": "/a:/etc"},
                                    {"com.docker.compose.project": "ok", "com.docker.compose.project.working_dir": "/a/../etc"},
                                    {"com.docker.compose.project": "ok", "com.docker.compose.project.working_dir": "/a\n/b"}])
def test_odd_project_labels_are_refused(client, fake, labels):
    fake.labels = labels
    r = client.post("/v0/run-updater", json={"tag": "edge"}, headers=H)
    assert r.status_code == 500
    assert not [q for q in fake.requests if q[0] == "POST"]
