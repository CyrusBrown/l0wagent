"""H3 end to end, no Docker: the fake hub (dev/hub), the gateway (gateway/), the tunnel service (tunnel/)
and control, each a subprocess. Control joins from the owner API; the instance is then reached at its
instance hostname through the hub and the tunnel, with previews on its files hostname; the self-check
passes; restarting the gateway, the tunnel or control loses nothing; rotating the key, then leaving
and rejoining, works.

`*.localhost` names may not resolve here, so every hub-bound connection goes to the hub's port with
the real Host header (L0W_HUB_CONNECT_TO and the tunnel config's connect_to)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest
from l0wproto import docs, payloads
from l0wproto.keys import Key

from l0w_control.auth import totp_code

from .stack import REPO, HubStack, Stack, wait_http

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to run the hub, gateway and tunnel projects")


HUB_DIR = REPO / "hub"
WRANGLER_PORT = 8787  # hub/wrangler.dev.toml's hostnames carry this port


class WranglerHubStack(HubStack):
    """The same chain with the real hub core (hub/, Cloudflare Workers) under `wrangler dev` instead of the fake hub."""

    def __init__(self, tmp):
        super().__init__(tmp, hub_port=WRANGLER_PORT)

    def start_hub(self) -> None:
        persist = self.tmp / "hub" / "wrangler-state"
        if not persist.exists():
            subprocess.run(["pnpm", "exec", "wrangler", "d1", "migrations", "apply", "DB", "--local", "--config", "wrangler.dev.toml", "--persist-to", str(persist)],
                           cwd=HUB_DIR, check=True, capture_output=True, env={**os.environ, "WRANGLER_SEND_METRICS": "false"})
        log = open(self.tmp / "hub.log", "ab")
        self.procs["hub"] = subprocess.Popen(["pnpm", "exec", "wrangler", "dev", "--config", "wrangler.dev.toml", "--local", "--port", str(WRANGLER_PORT), "--persist-to", str(persist)],
                                             cwd=HUB_DIR, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env={**os.environ, "WRANGLER_SEND_METRICS": "false"})
        wait_http(f"http://127.0.0.1:{WRANGLER_PORT}/.well-known/l0wagent/hub", {"Host": f"hub.localhost:{WRANGLER_PORT}"}, timeout=90)

    def invite(self) -> str:
        out = subprocess.run(["node", str(HUB_DIR / "scripts" / "invite.mjs"), "--hub", self.hub_base, "--connect", f"127.0.0.1:{WRANGLER_PORT}"],
                             capture_output=True, text=True, check=True, env={**os.environ, "HUB_ADMIN_TOKEN": "dev-admin-token"})
        return [ln for ln in out.stdout.splitlines() if ln.startswith("l0w-invite:")][-1]


def _wrangler_ready() -> str | None:
    if not (HUB_DIR / "node_modules" / ".bin" / "wrangler").exists():
        return "hub/node_modules missing (cd hub && pnpm install)"
    for _ in range(60):  # the previous test's wrangler may still be letting go of the port
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", WRANGLER_PORT)) != 0:
                return None
        time.sleep(0.25)
    return f"port {WRANGLER_PORT} is busy"


@pytest.fixture(params=["fakehub", "hub-core"])
def hs(request, tmp_path):
    if request.param == "hub-core" and (why := _wrangler_ready()):
        pytest.skip(why)
    st = (HubStack if request.param == "fakehub" else WranglerHubStack)(tmp_path).up()
    yield st
    st.down()


def enable_totp(st: Stack) -> None:
    secret = st.client.post("/api/account/totp/setup").json()["secret"]
    r = st.client.post("/api/account/totp/enable", json={"code": totp_code(secret, int(time.time()) // 30)})
    assert r.status_code == 200, r.text


def test_join_reach_check_rotate_leave_rejoin(hs: HubStack):
    hs.login()
    c = hs.client
    caps0 = c.get("/api/meta").json()["capabilities"]
    assert caps0["hub"] is False and caps0["hub_join"] is True
    assert c.get("/api/membership").json()["state"] == "standalone"
    assert c.get("/.well-known/l0wagent/self").status_code == 404

    invite = hs.invite()
    pv = c.post("/api/membership/preview", json={"invite": invite}).json()
    assert pv["hub"]["domain"] == f"hub.localhost:{hs.hub_port}" or pv["hub"]["domain"].startswith("hub.localhost")
    assert pv["two_factor_ready"] is False and pv["problems"]
    r = c.post("/api/membership/join", json={"invite": invite, "name": "mara", "backup": "skipped", "idempotency_key": "j1"})
    assert r.status_code == 400 and r.json()["error"] == "needs_2fa"

    enable_totp(hs)
    r = c.post("/api/membership/join", json={"invite": invite, "name": "mara", "backup": "made", "idempotency_key": "j2"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["recovery_key"].startswith("l0w-recovery:")
    m = res["membership"]
    assert m["state"] == "joined" and m["address"].startswith("mara@")
    assert m["tunnel"]["state"] == "connected", m
    assert m["check"]["ok"], m["check"]
    inst, files = m["hostnames"]["instance"], m["hostnames"]["files"]
    assert c.get("/api/meta").json()["capabilities"]["hub"] is True

    # Reached from outside, through the hub and the tunnel: the UI's API, the self-description, the files app.
    assert hs.visit(inst, "/api/meta").json()["instance_name"]
    selfdoc = hs.visit(inst, "/.well-known/l0wagent/self").json()
    p = payloads.verify_self(selfdoc)
    assert p["hostnames"] == m["hostnames"]
    assert "sandbox" in hs.visit(files, "/").headers.get("content-security-policy", "")
    # The directory serves our key, countersigned by the hub.
    caps = payloads.verify_hub(hs.visit(f"hub.localhost:{hs.hub_port}", "/.well-known/l0wagent/hub").json())
    hub_key = next(Key.from_dict(k) for k in caps["keys"] if k["use"] == "sign")
    entry = hs.visit(f"hub.localhost:{hs.hub_port}", "/.well-known/l0wagent/members/mara").json()
    assert docs.verify_directory_entry(entry, hub_key, hub_domain=caps["domain"], name="mara")["keys"] == p["keys"]

    # Previews and the connector's issuer use the hub's hostnames when reached through it.
    issuer = hs.visit(inst, "/.well-known/oauth-authorization-server").json()["issuer"]
    assert issuer == f"http://{inst}"
    # The connector through the hub (H4): discovery and the 401 challenge name the instance hostname.
    assert hs.visit(inst, "/.well-known/oauth-protected-resource").json()["resource"] == f"http://{inst}/mcp"
    r = httpx.post(f"http://127.0.0.1:{hs.hub_port}/mcp", headers={"Host": inst, "Content-Type": "application/json"}, json={}, timeout=15)
    assert r.status_code == 401 and f"http://{inst}/.well-known/oauth-protected-resource" in r.headers.get("www-authenticate", "")
    # A preview link asked for through the hub points at the files hostname.

    # Restarts lose nothing: gateway, tunnel, control.
    for name, start in (("gateway", hs.start_gateway), ("tunnel", hs.start_tunnel), ("control", hs.start_control)):
        hs.kill(name)
        start()
        hs.wait_for(lambda: hs.visit(inst, "/api/meta").status_code == 200, timeout=40, what=f"reachable after restarting {name}")
    assert c.post("/api/membership/check").json()["ok"]

    # The hub goes down: the instance keeps working on the LAN, the tunnel shows offline, then reconnects.
    hs.kill("hub")
    assert c.get("/api/status").status_code == 200
    hs.wait_for(lambda: c.get("/api/membership").json()["tunnel"]["state"] in ("offline", "connecting"), timeout=30, what="tunnel offline")
    failed = c.post("/api/membership/check").json()
    assert not failed["ok"] and failed["problems"]
    hs.start_hub()
    hs.wait_for(lambda: c.get("/api/membership").json()["tunnel"]["state"] == "connected", timeout=90, what="tunnel back")
    assert c.post("/api/membership/check").json()["ok"]

    # Rotate: the directory follows, with the rotation in the history.
    old_kid = c.get("/api/membership").json()["sign_key"]["kid"]
    m2 = c.post("/api/membership/rotate").json()
    assert m2["sign_key"]["kid"] != old_kid
    entry = hs.visit(f"hub.localhost:{hs.hub_port}", "/.well-known/l0wagent/members/mara").json()
    p2 = docs.verify_directory_entry(entry, hub_key, hub_domain=caps["domain"], name="mara")
    assert p2["history"] and p2["seq"] > p["seq"]
    hs.wait_for(lambda: hs.visit(inst, "/api/meta").status_code == 200, timeout=30, what="tunnel after rotation")
    assert c.post("/api/membership/check").json()["ok"]

    # Recover with the recovery key (a compromise rotation).
    bad = c.post("/api/membership/recover", json={"recovery_key": "l0w-recovery:" + "A" * 43})
    assert bad.status_code == 400 and bad.json()["error"] == "wrong_recovery_key"
    assert c.post("/api/membership/recover", json={"recovery_key": res["recovery_key"]}).status_code == 200

    # Leave (typed confirmation), then rejoin under a new name with a new invite.
    assert c.post("/api/membership/leave", json={"confirm": "someone@else"}).status_code == 400
    left = c.post("/api/membership/leave", json={"confirm": m["address"]}).json()
    assert left["state"] == "left"
    assert hs.visit(f"hub.localhost:{hs.hub_port}", "/.well-known/l0wagent/members/mara").status_code == 410
    assert c.get("/.well-known/l0wagent/self").status_code == 404
    r = c.post("/api/membership/join", json={"invite": hs.invite(), "name": "mara2", "backup": "made", "idempotency_key": "j3"})
    assert r.status_code == 200, r.text
    m3 = r.json()["membership"]
    assert m3["check"]["ok"], m3
    assert hs.visit(m3["hostnames"]["instance"], "/api/meta").status_code == 200


def test_join_refuses_taken_name(hs: HubStack):
    hs.login()
    enable_totp(hs)
    c = hs.client
    assert c.post("/api/membership/join", json={"invite": hs.invite(), "name": "admin", "backup": "made", "idempotency_key": "x"}).json()["error"] == "name_reserved"
    r = c.post("/api/membership/join", json={"invite": "l0w-invite:nope", "name": "mara", "backup": "made", "idempotency_key": "y"})
    assert r.status_code == 400 and r.json()["error"] == "bad_invite"
