"""H6, control's side of the ops container (ops.py): Update now, installing extensions from a git URL, and a hub service's extension.
Ops itself is a fake here (an httpx MockTransport that keeps the extensions folder the way the real one does); the real one is
tested in ops/tests and joined to control by compose."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import httpx
import pytest
from l0wproto import docs, payloads

from l0w_control import auth
from l0w_control.ops import OpsClient
from l0w_control.updates import draining, set_drain

from .hubworld import AGENT, BOARD, ECHO, ok

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
GIT = "https://git.example/hub-services.git"


class FakeOps:
    """What ops answers, and what it was asked. It writes real folders into the extensions directory, as ops does."""

    def __init__(self, extensions_dir):
        self.dir = extensions_dir
        self.calls: list[tuple[str, str, dict | None]] = []
        self.docker = True
        self.progress: dict | None = None
        self.update_error: tuple[int, str, str] | None = None
        self.install_error: tuple[int, str, str] | None = None
        self.resolve_commit = COMMIT_A
        self.toml_left = False

    def bodies(self, path: str) -> list[dict]:
        return [b for _, p, b in self.calls if p == path and b is not None]

    def _installed(self) -> list[dict]:
        out = []
        for d in sorted(self.dir.iterdir()):
            f = d / ".l0w-source.json"
            if d.is_dir() and f.is_file():
                out.append({"name": d.name, **{k: v for k, v in json.loads(f.read_text()).items() if k in ("git", "commit", "path", "source", "installed_at")}})
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ops-token"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/v0/status":
            return httpx.Response(200, json={"docker": self.docker, "docker_enabled": self.docker, "channel": "stable", "extensions": self._installed(),
                                             "update": None, "update_progress": self.progress})
        if path == "/v0/extensions/resolve":
            if not body["git"].startswith("https://"):
                return httpx.Response(400, json={"error": "bad_url", "detail": "git must be an https URL"})
            return httpx.Response(200, json={"commit": self.resolve_commit})
        if path == "/v0/extensions/install":
            if self.install_error:
                status, code, detail = self.install_error
                return httpx.Response(status, json={"error": code, "detail": detail})
            d = self.dir / body["name"]
            d.mkdir(exist_ok=True)
            (d / "extension.toml").write_text(f'name = "{body["name"]}"\nversion = "1.0.0"\nsdk = "1.0"\ntitle = "{body["name"].title()}"\n')
            (d / ".l0w-source.json").write_text(json.dumps({k: body.get(k) for k in ("git", "commit", "path", "source")} | {"installed_at": "2026-11-22T10:00:00Z"}))
            return httpx.Response(200, json={"name": body["name"], "commit": body["commit"], "source": body["source"]})
        if path == "/v0/extensions/remove":
            import shutil

            shutil.rmtree(self.dir / body["name"], ignore_errors=True)
            return httpx.Response(200, json={"name": body["name"], "removed": True, "toml_entry_removed": False, "still_in_extensions_toml": self.toml_left})
        if path == "/v0/update":
            if self.update_error:
                status, code, detail = self.update_error
                return httpx.Response(status, json={"error": code, "detail": detail})
            self.progress = {"version": body["version"], "phase": "updater_started"}
            return httpx.Response(200, json={"started": True})
        return httpx.Response(404, json={"error": "not_found", "detail": "no such endpoint"})


@pytest.fixture
def ops(hw, tmp_path):
    fake = FakeOps(tmp_path / "extensions")
    hw.be.ops.client = OpsClient("http://ops:8795", "ops-token", transport=httpx.MockTransport(fake.handler))
    hw.be.ops.poll = 0.01
    hw.fake = fake
    hw.ops = hw.be.ops
    return hw


def offer(w, version: str = "9.9.9") -> None:
    w.be.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('update_check', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (json.dumps({"checked_at": auth.iso(auth.utcnow()), "latest": version, "notes": "Notes", "url": None, "error": None}), w.be.db.now()))


def wait_for(cond, what: str = "the condition", timeout: float = 5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def info(w) -> dict:
    return ok(w.c.get("/api/system/update"))


def apply(w, version: str = "9.9.9", key: str = "u-1"):
    return w.c.post("/api/system/update/apply", json={"version": version, "idempotency_key": key})


# ── Update now ──────────────────────────────────────────────────────────────
def test_without_ops_everything_hides(hw):
    assert hw.be.ops.client is None
    assert ok(hw.c.get("/api/meta"))["capabilities"]["ops"] is False
    got = info(hw)
    assert got["ops_available"] is False and got["ops_docker"] is False and got["applying"] is None and got["last_result"] is None
    assert apply(hw).status_code == 409
    assert hw.c.post("/api/extensions/resolve", json={"git": "https://x.example/r.git"}).status_code == 409


def test_update_info_says_whether_ops_can_update(ops):
    assert ok(ops.c.get("/api/meta"))["capabilities"]["ops"] is True
    assert info(ops)["ops_available"] is True and info(ops)["ops_docker"] is False  # not looked yet
    ops.run(ops.ops.refresh_status())
    assert info(ops)["ops_docker"] is True
    ops.fake.docker = False
    ops.run(ops.ops.refresh_status())
    assert info(ops)["ops_available"] is True and info(ops)["ops_docker"] is False


def test_update_is_refused_without_docker_or_for_another_version(ops):
    offer(ops)
    ops.fake.docker = False
    r = apply(ops)
    assert r.status_code == 409 and r.json()["error"] == "updates_off"
    ops.fake.docker = True
    assert apply(ops, "9.9.8", "u-2").json()["error"] == "not_offered"
    assert apply(ops, "0.0.1", "u-3").json()["error"] == "not_offered"
    assert draining(ops.be.db) is None and not ops.fake.bodies("/v0/update")
    ops.be.db.ex("DELETE FROM kv WHERE key='update_check'")  # no update offered at all
    assert apply(ops, "9.9.9", "u-4").json()["error"] == "not_offered"


def test_update_drains_waits_for_idle_then_asks_ops(ops, monkeypatch):
    offer(ops)
    waiting = ["run #7 (wakeup) is in progress"]
    monkeypatch.setattr("l0w_control.ops.blockers", lambda db: list(waiting))
    got = ok(apply(ops))
    assert got["applying"]["state"] == "waiting" and got["draining_until"] and "Waiting for the assistant" in got["applying"]["step"]
    assert draining(ops.be.db) is not None  # no new run starts meanwhile
    assert apply(ops, key="u-again").status_code == 409  # one at a time
    assert ok(apply(ops))["applying"]["started_at"] == got["applying"]["started_at"]  # the same key is the same answer, not a second update
    time.sleep(0.2)
    assert not ops.fake.bodies("/v0/update")  # still waiting for the run
    assert "run #7" in info(ops)["applying"]["step"]
    waiting.clear()
    wait_for(lambda: ops.fake.bodies("/v0/update"), "ops being asked")
    assert ops.fake.bodies("/v0/update") == [{"version": "9.9.9"}]
    wait_for(lambda: info(ops)["applying"] and info(ops)["applying"]["state"] == "restarting", "the restart step")
    assert draining(ops.be.db) is not None  # still drained while the stack is replaced
    # The updater finishes (it wrote its result into the extensions folder); control's new process reads it.
    (ops.fake.dir / ".l0w-update.json").write_text(json.dumps({"tag": "9.9.9", "started_at": auth.iso(auth.utcnow()), "finished_at": auth.iso(auth.utcnow() + timedelta(seconds=30)),
                                                              "ok": True, "before": "0.1.0", "after": "9.9.9", "tail": "Updated."}))
    done = wait_for(lambda: info(ops)["last_result"], "the result")
    assert done["ok"] is True and done["version"] == "9.9.9" and info(ops)["applying"] is None
    assert draining(ops.be.db) is None  # the updater ran with --no-drain: control lets runs start again
    notes = ops.be.db.q("SELECT * FROM notifications WHERE kind='update'")
    assert len(notes) == 1 and "9.9.9" in notes[0]["title"]


def test_a_failed_update_clears_the_drain_and_says_why(ops):
    offer(ops)
    ops.fake.update_error = (502, "release_check_failed", "couldn't reach the release list")
    ok(apply(ops))
    done = wait_for(lambda: info(ops)["last_result"], "the failure")
    assert done["ok"] is False and "release list" in done["error"] and info(ops)["applying"] is None
    assert draining(ops.be.db) is None
    n = ops.be.db.q("SELECT * FROM notifications WHERE kind='update'")
    assert len(n) == 1 and "failed" in n[0]["title"]
    # ... and the owner can try again (a new key, since the first was used).
    ops.fake.update_error = None
    ok(apply(ops, key="u-retry"))
    wait_for(lambda: ops.fake.bodies("/v0/update"), "the second try")


def test_an_updater_that_fails_is_reported_with_its_output(ops):
    offer(ops)
    ok(apply(ops))
    wait_for(lambda: info(ops)["applying"] and info(ops)["applying"]["state"] == "restarting", "restarting")
    (ops.fake.dir / ".l0w-update.json").write_text(json.dumps({"tag": "9.9.9", "finished_at": auth.iso(auth.utcnow() + timedelta(seconds=5)), "ok": False,
                                                              "tail": "pull access denied for l0wagent-control"}))
    done = wait_for(lambda: info(ops)["last_result"], "the result")
    assert done["ok"] is False and "pull access denied" in done["error"] and draining(ops.be.db) is None


def test_ops_failing_to_start_the_updater_is_a_failure(ops):
    offer(ops)
    ops.fake.progress = None
    real = ops.fake.handler

    def failing(request):
        r = real(request)
        if request.url.path == "/v0/update":
            ops.fake.progress = {"version": "9.9.9", "phase": "failed", "error": "docker pull failed: no space left"}
        return r

    ops.ops.client.transport = httpx.MockTransport(failing)
    ok(apply(ops))
    done = wait_for(lambda: info(ops)["last_result"], "the failure")
    assert done["ok"] is False and "no space left" in done["error"] and draining(ops.be.db) is None


def test_waiting_too_long_for_idle_gives_up_and_lets_runs_start(ops, monkeypatch):
    offer(ops)
    monkeypatch.setattr("l0w_control.ops.blockers", lambda db: ["run #7 (wakeup) is in progress"])
    ops.be.settings.set("UPDATE_IDLE_WAIT_MINUTES", 5)
    clock = {"t": auth.utcnow()}
    ops.ops.now = lambda: clock["t"]
    ok(apply(ops))
    time.sleep(0.1)
    assert info(ops)["applying"]["state"] == "waiting"
    clock["t"] += timedelta(minutes=6)
    done = wait_for(lambda: info(ops)["last_result"], "the timeout")
    assert done["ok"] is False and "5 minutes" in done["error"] and "run #7" in done["error"]
    assert draining(ops.be.db) is None and not ops.fake.bodies("/v0/update")


def test_the_owner_can_cancel_a_wait_but_not_a_running_update(ops, monkeypatch):
    offer(ops)
    monkeypatch.setattr("l0w_control.ops.blockers", lambda db: ["run #7 (wakeup) is in progress"])
    ok(apply(ops))
    got = ok(ops.c.post("/api/system/update/cancel"))
    assert got["applying"] is None and got["draining_until"] is None
    time.sleep(0.1)
    assert not ops.fake.bodies("/v0/update")
    monkeypatch.setattr("l0w_control.ops.blockers", lambda db: [])
    ok(apply(ops, key="u-2"))
    wait_for(lambda: info(ops)["applying"] and info(ops)["applying"]["state"] == "restarting", "restarting")
    assert ops.c.post("/api/system/update/cancel").status_code == 409


def test_a_restarted_control_picks_the_update_up(ops):
    """Control is replaced in the middle of an update: the new process finds the state in the database and the updater's result in the extensions folder."""
    ops.ops._kv_set("update_apply", {"version": "9.9.9", "state": "restarting", "step": "Restarting the app", "started_at": auth.iso(auth.utcnow() - timedelta(minutes=2))})
    set_drain(ops.be.db, True, by="web")
    (ops.fake.dir / ".l0w-update.json").write_text(json.dumps({"tag": "9.9.9", "finished_at": auth.iso(auth.utcnow()), "ok": True, "tail": ""}))
    ops.run(ops.ops.startup(ops.be.loop))
    done = wait_for(lambda: info(ops)["last_result"], "the result")
    assert done["ok"] is True and info(ops)["applying"] is None and draining(ops.be.db) is None
    # shown for a day, not for ever
    ops.be.db.ex("UPDATE kv SET value=? WHERE key='update_result'", (json.dumps({"version": "9.9.9", "ok": True, "error": None, "at": auth.iso(auth.utcnow() - timedelta(days=2))}),))
    assert info(ops)["last_result"] is None


def test_a_device_token_with_cli_scope_cannot_start_an_update():
    assert not auth.scope_allows("cli", "POST", "/api/system/update/apply")
    assert not auth.scope_allows("read", "POST", "/api/system/update/apply")
    assert auth.scope_allows("full", "POST", "/api/system/update/apply")


# ── extensions from a git URL ───────────────────────────────────────────────
def test_resolve_install_and_remove(ops):
    got = ok(ops.c.post("/api/extensions/resolve", json={"git": "https://git.example/plantlog.git", "ref": "v1.0"}))
    assert got == {"commit": COMMIT_A}
    assert ops.fake.bodies("/v0/extensions/resolve") == [{"git": "https://git.example/plantlog.git", "ref": "v1.0"}]
    assert ops.c.post("/api/extensions/resolve", json={"git": "http://git.example/x.git"}).status_code == 400  # ops's own refusal, in its words
    req = {"name": "plantlog", "git": "https://git.example/plantlog.git", "commit": COMMIT_A, "path": "extensions/plantlog", "idempotency_key": "i-1"}
    made = ok(ops.c.post("/api/extensions/install", json=req), 201)
    assert made["name"] == "plantlog" and made["enabled"] is False and made["ops"]["source"] == "owner" and made["ops"]["commit"] == COMMIT_A
    assert ops.fake.bodies("/v0/extensions/install")[0] == {"name": "plantlog", "git": req["git"], "commit": COMMIT_A, "path": "extensions/plantlog", "source": "owner"}
    ok(ops.c.post("/api/extensions/install", json=req), 201)  # the same key: not installed twice
    assert len(ops.fake.bodies("/v0/extensions/install")) == 1
    listed = {e["name"]: e for e in ok(ops.c.get("/api/extensions"))}
    assert listed["plantlog"]["ops"]["source"] == "owner" and listed["plantlog"]["present"] is True
    gone = ok(ops.c.delete("/api/extensions/plantlog"))
    assert gone == {"name": "plantlog", "still_in_extensions_toml": False}
    assert ops.fake.bodies("/v0/extensions/remove") == [{"name": "plantlog"}]
    assert next(e for e in ok(ops.c.get("/api/extensions")) if e["name"] == "plantlog")["present"] is False  # the row stays, so its data does


def test_removing_a_toml_extension_says_when_the_file_still_lists_it(ops):
    d = ops.fake.dir / "from-toml"
    d.mkdir()
    (d / "extension.toml").write_text('name = "from-toml"\nversion = "1"\nsdk = "1.0"\ntitle = "T"\n')
    (d / ".l0w-source.json").write_text(json.dumps({"git": "https://git.example/x.git", "commit": COMMIT_A, "path": None, "source": "toml"}))
    ops.fake.toml_left = True
    ops.be.ext.scan()
    assert ok(ops.c.delete("/api/extensions/from-toml"))["still_in_extensions_toml"] is True


def test_only_what_ops_installed_can_be_removed_here(ops):
    d = ops.fake.dir / "mine"
    d.mkdir()
    (d / "extension.toml").write_text('name = "mine"\nversion = "1"\nsdk = "1.0"\ntitle = "Mine"\n')
    ops.be.ext.scan()
    r = ops.c.delete("/api/extensions/mine")
    assert r.status_code == 409 and r.json()["error"] == "not_ours" and (d / "extension.toml").exists()
    assert not ops.fake.bodies("/v0/extensions/remove")


def test_an_ops_refusal_reaches_the_owner_in_ops_words(ops):
    ops.fake.install_error = (403, "toml_mismatch", "git, commit and path must be exactly what [extension.x] says")
    r = ops.c.post("/api/extensions/install", json={"name": "plantlog", "git": "https://git.example/p.git", "commit": COMMIT_A, "idempotency_key": "i-2"})
    assert r.status_code == 400 and "exactly what" in r.json()["detail"]
    ops.fake.install_error = (502, "git_failed", "couldn't fetch")
    assert ops.c.post("/api/extensions/install", json={"name": "plantlog", "git": "https://git.example/p.git", "commit": COMMIT_A, "idempotency_key": "i-3"}).status_code == 409


def test_extension_routes_are_the_owners_only(ops):
    from fastapi.testclient import TestClient

    sandbox = TestClient(ops.c.app, client=("10.255.255.5", 5000), headers={**AGENT, "x-l0w-client": "agent"})
    for method, path in (("POST", "/api/extensions/resolve"), ("POST", "/api/extensions/install"), ("DELETE", "/api/extensions/plantlog"), ("POST", "/api/system/update/apply"),
                         ("PATCH", "/api/membership")):
        assert sandbox.request(method, path, json={}).status_code in (401, 403, 404)
    assert not ops.fake.calls


# ── a hub service's extension ───────────────────────────────────────────────
def with_extension(w, commit: str = COMMIT_A, *, default: str = "on", path: str = "extensions/board") -> dict:
    """The hub lists echo, and board with an extension at `commit`. -> the board document (as listed)."""
    p = payloads.build_service(BOARD, "board", ["moderator", "poster"], title="Board", description="A service.", default=default,
                               extension={"git": GIT, "commit": commit, "path": path})
    board = {"kind": "service", "doc": docs.sign_document("service", p, [w.hub])}
    w.set_caps(w.service_doc(ECHO), board)
    return board["doc"]


def settle(w) -> None:
    w.run(w.ops.idle())


def board_row(w) -> dict:
    return w.row(BOARD)


def test_a_service_extension_installs_with_the_signed_document_and_the_pinned_key(ops):
    doc = with_extension(ops)
    ops.sync()
    settle(ops)
    install = ops.fake.bodies("/v0/extensions/install")
    assert len(install) == 1
    assert install[0] == {"name": "board", "git": GIT, "commit": COMMIT_A, "path": "extensions/board", "source": "hub",
                          "service_doc": json.loads(json.dumps(doc)), "hub_key": ops.hub.key.to_dict()}
    # What ops is given is what ops can check: the document verifies against the key sent with it, and names this git, commit and path.
    from l0wproto.keys import Key

    verified = payloads.verify_service(install[0]["service_doc"], Key.from_dict(install[0]["hub_key"]))
    assert verified["extension"] == {"git": GIT, "commit": COMMIT_A, "path": "extensions/board"}
    row = board_row(ops)
    assert row["ext_state"] == "installed" and row["ext_commit"] == COMMIT_A and row["extension_enabled"] == 1
    got = {x["address"]: x for x in ok(ops.c.get("/api/membership/services"))}[BOARD]
    assert got["extension_name"] == "board" and got["extension_state"] == "installed" and got["extension_commit"] == COMMIT_A and got["extension_enabled"] is True
    assert got["enabled"] is True
    listed = {e["name"]: e for e in ok(ops.c.get("/api/extensions"))}
    assert listed["board"]["ops"]["source"] == "hub" and listed["board"]["source"].startswith("hub: ")  # the Extensions list shows it, with its hub
    assert listed["board"]["enabled"] is True  # a first install is turned on: the owner's "on" for the service's extension
    assert any(e["event"] == "extension" and "installed" in e["detail"] for e in ops.be.db.q("SELECT * FROM hub_log"))
    assert ECHO not in {r["address"] for r in ops.be.db.q("SELECT address FROM hub_services WHERE ext_state!='off'")}  # echo names no extension


def test_the_extension_has_its_own_switch_and_a_service_off_by_default_installs_nothing(ops):
    with_extension(ops, default="off")
    ops.sync()
    settle(ops)
    assert board_row(ops)["enabled"] == 0 and board_row(ops)["extension_enabled"] == 0
    assert not ops.fake.bodies("/v0/extensions/install")
    on = ok(ops.c.patch(f"/api/membership/services/{BOARD}", json={"extension_enabled": True}))
    assert on["extension_enabled"] is True and on["enabled"] is False and on["extension_state"] == "installing"
    settle(ops)
    assert len(ops.fake.bodies("/v0/extensions/install")) == 1 and board_row(ops)["ext_state"] == "installed"
    assert board_row(ops)["enabled"] == 0  # the service's own switch didn't move
    off = ok(ops.c.patch(f"/api/membership/services/{BOARD}", json={"extension_enabled": False}))
    assert off["extension_enabled"] is False
    settle(ops)
    assert ops.fake.bodies("/v0/extensions/remove") == [{"name": "board"}]
    assert board_row(ops)["ext_state"] == "off" and not (ops.fake.dir / "board").exists()
    assert ops.c.patch(f"/api/membership/services/{ECHO}", json={"extension_enabled": True}).status_code == 409  # echo has none
    assert ops.c.patch(f"/api/membership/services/{BOARD}", json={}).status_code == 400


def test_an_install_that_ops_refuses_shows_its_error_and_is_tried_again(ops):
    with_extension(ops)
    ops.fake.install_error = (403, "doc_mismatch", "git, commit and path must be exactly what the hub's service document lists")
    ops.sync()
    settle(ops)
    row = board_row(ops)
    assert row["ext_state"] == "error" and "exactly what the hub's service document" in row["ext_error"]
    ops.fake.install_error = None
    ops.sync()
    settle(ops)
    assert board_row(ops)["ext_state"] == "installed" and board_row(ops)["ext_error"] is None


def test_a_new_commit_from_the_hub_is_applied_and_logged(ops):
    with_extension(ops)
    ops.sync()
    settle(ops)
    ops.be.db.ex("DELETE FROM notifications")
    doc = with_extension(ops, COMMIT_B)
    ops.sync()
    settle(ops)
    assert [b["commit"] for b in ops.fake.bodies("/v0/extensions/install")] == [COMMIT_A, COMMIT_B]
    assert ops.fake.bodies("/v0/extensions/install")[1]["service_doc"] == json.loads(json.dumps(doc))
    assert board_row(ops)["ext_commit"] == COMMIT_B and board_row(ops)["ext_state"] == "installed"
    log = [e["detail"] for e in ops.be.db.q("SELECT * FROM hub_log WHERE event='extension'")]
    assert any("updated" in d and COMMIT_A[:10] in d and COMMIT_B[:10] in d for d in log)
    notes = ops.be.db.q("SELECT * FROM notifications WHERE kind='hub'")
    assert len(notes) == 1 and "extension updated" in notes[0]["title"]
    ops.sync()  # nothing new: nothing to do
    settle(ops)
    assert len(ops.fake.bodies("/v0/extensions/install")) == 2


def test_ask_first_files_one_card_and_installs_only_when_allowed(ops):
    with_extension(ops)
    ops.sync()
    settle(ops)
    m = ok(ops.c.patch("/api/membership", json={"ask_before_extension_updates": True}))
    assert m["ask_before_extension_updates"] is True
    with_extension(ops, COMMIT_B)
    ops.sync()
    settle(ops)
    assert len(ops.fake.bodies("/v0/extensions/install")) == 1  # the old commit stays
    assert board_row(ops)["ext_state"] == "waiting" and board_row(ops)["ext_commit"] == COMMIT_A
    ops.sync()
    settle(ops)  # asked once for this commit
    cards = [e for e in ok(ops.c.get("/api/inbox")) if e["kind"] == "permission"]
    assert len(cards) == 1
    perm = cards[0]["permission"]
    act = perm["actions"][0]
    assert act["kind"] == "extension_update" and perm["scopes"] == ["once"] and COMMIT_A[:10] in act["summary"] and COMMIT_B[:10] in act["summary"]
    assert act["payload"]["address"] == BOARD and act["payload"]["to_commit"] == COMMIT_B
    assert any(n["kind"] == "approval" for n in ops.be.db.q("SELECT * FROM notifications"))
    # The agent never sees it, can't withdraw it, and it isn't told the decision.
    assert all(x["id"] != perm["id"] for x in ok(ops.c.get("/api/agent/asks", headers=AGENT)) if x["kind"] == "permission")
    assert ops.c.post(f"/api/agent/permissions/{perm['id']}/withdraw", json={}, headers=AGENT).status_code == 409
    ok(ops.c.post(f"/api/permissions/{perm['id']}/decide", json={"decision": "allow", "scope": "once"}))
    settle(ops)
    assert [b["commit"] for b in ops.fake.bodies("/v0/extensions/install")] == [COMMIT_A, COMMIT_B]
    assert board_row(ops)["ext_state"] == "installed" and board_row(ops)["ext_commit"] == COMMIT_B


def test_a_denied_update_stays_denied_until_the_hub_publishes_another(ops):
    with_extension(ops)
    ops.sync()
    settle(ops)
    ok(ops.c.patch("/api/membership", json={"ask_before_extension_updates": True}))
    with_extension(ops, COMMIT_B)
    ops.sync()
    settle(ops)
    pid = ops.be.db.q1("SELECT id FROM permissions")["id"]
    ok(ops.c.post(f"/api/permissions/{pid}/decide", json={"decision": "deny"}))
    ops.sync()
    settle(ops)
    assert len(ops.be.db.q("SELECT * FROM permissions")) == 1 and len(ops.fake.bodies("/v0/extensions/install")) == 1  # not asked again
    with_extension(ops, "c" * 40)
    ops.sync()
    settle(ops)
    assert len(ops.be.db.q("SELECT * FROM permissions")) == 2  # a newer commit is a new question
    # "Update now" on the service row is the owner agreeing without the card.
    ok(ops.c.patch(f"/api/membership/services/{BOARD}", json={"extension_enabled": True}))
    settle(ops)
    assert board_row(ops)["ext_commit"] == "c" * 40 and board_row(ops)["ext_state"] == "installed"


def test_turning_ask_first_off_applies_what_was_waiting(ops):
    with_extension(ops)
    ops.sync()
    settle(ops)
    ok(ops.c.patch("/api/membership", json={"ask_before_extension_updates": True}))
    with_extension(ops, COMMIT_B)
    ops.sync()
    settle(ops)
    assert board_row(ops)["ext_state"] == "waiting"
    ok(ops.c.patch("/api/membership", json={"ask_before_extension_updates": False}))
    settle(ops)
    assert board_row(ops)["ext_commit"] == COMMIT_B


def test_the_agent_cannot_file_an_extension_update(ops):
    for action in ({"kind": "extension_update", "address": BOARD, "name": "board", "from_commit": COMMIT_A, "to_commit": COMMIT_B},
                   {"kind": "extension_update", "address": BOARD, "to_commit": COMMIT_B, "payload": {"to_commit": COMMIT_B}}):
        r = ops.c.post("/api/agent/permissions", headers=AGENT, json={"text": "update the board extension", "actions": [action]})
        assert r.status_code == 400
    assert ops.be.db.q("SELECT * FROM permissions") == []


def test_a_service_the_hub_drops_or_leaving_the_hub_removes_what_it_installed(ops):
    with_extension(ops)
    ops.sync()
    settle(ops)
    assert (ops.fake.dir / "board").is_dir()
    ops.set_caps(ops.service_doc(ECHO))  # the hub stopped listing board
    ops.sync()
    settle(ops)
    assert ops.fake.bodies("/v0/extensions/remove") == [{"name": "board"}] and not (ops.fake.dir / "board").exists()
    assert any("removed" in e["detail"] for e in ops.be.db.q("SELECT * FROM hub_log WHERE event='extension'"))
    with_extension(ops)
    ops.sync()
    settle(ops)
    assert (ops.fake.dir / "board").is_dir()
    ops.run(ops.hs.forget_all())  # leaving the hub
    settle(ops)
    assert not (ops.fake.dir / "board").exists()


def test_a_folder_that_is_not_ops_s_is_left_alone(ops):
    d = ops.fake.dir / "board"
    d.mkdir()
    (d / "extension.toml").write_text('name = "board"\nversion = "1"\nsdk = "1.0"\ntitle = "Mine"\n')
    ops.fake.install_error = (409, "not_ours", "extensions/board wasn't installed by ops")
    with_extension(ops)
    ops.sync()
    settle(ops)
    assert board_row(ops)["ext_state"] == "error" and "wasn't installed by ops" in board_row(ops)["ext_error"]
    assert (d / "extension.toml").exists() and not ops.fake.bodies("/v0/extensions/remove")


def test_ops_being_down_is_an_error_on_the_row_not_a_crash(ops):
    with_extension(ops)
    ops.ops.client.transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    ops.sync()
    settle(ops)
    assert board_row(ops)["ext_state"] == "error" and "isn't answering" in board_row(ops)["ext_error"]
    ops.ops.client.transport = httpx.MockTransport(ops.fake.handler)
    ops.sync()
    settle(ops)
    assert board_row(ops)["ext_state"] == "installed"


def test_without_ops_a_service_extension_is_listed_but_nothing_installs(hw):
    p = payloads.build_service(BOARD, "board", ["poster"], title="Board", default="on", extension={"git": GIT, "commit": COMMIT_A, "path": "."})
    hw.set_caps(hw.service_doc(ECHO), {"kind": "service", "doc": docs.sign_document("service", p, [hw.hub])})
    hw.sync()
    got = {x["address"]: x for x in ok(hw.c.get("/api/membership/services"))}[BOARD]
    assert got["extension"]["commit"] == COMMIT_A and got["extension_state"] == "off" and got["extension_enabled"] is True
    assert hw.be.db.q1("SELECT doc_json FROM hub_services WHERE address=?", (BOARD,))["doc_json"]  # the signed document is kept for later
