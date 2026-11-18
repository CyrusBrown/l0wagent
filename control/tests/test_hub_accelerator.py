"""The hub's cloudflared accelerator (protocol/accelerators.md): parsing the capabilities extra, the setting,
the tunnel settings pushed to the gateway, and the status coming back."""

import asyncio
import json
from types import SimpleNamespace

from l0w_control.db import Database
from l0w_control.membership import Membership, accelerator_api

API = "https://hub.example/v0/accelerators/cloudflared"
EXTRA = {"kind": "accelerator", "id": "cloudflared", "api": API}


def test_parse_extra():
    assert accelerator_api({"extras": [EXTRA]}) == API
    assert accelerator_api({"extras": []}) is None and accelerator_api({}) is None and accelerator_api(None) is None
    assert accelerator_api({"extras": [{"kind": "accelerator", "id": "cloudflared"}]}) is None
    assert accelerator_api({"extras": [{**EXTRA, "api": "http://hub.example/x"}]}) is None
    assert accelerator_api({"extras": [{**EXTRA, "api": "http://127.0.0.1:8770/x"}]}, allow_http=True) == "http://127.0.0.1:8770/x"
    assert accelerator_api({"extras": [{"kind": "accelerator", "id": "other", "api": API}]}) is None


class FakeGateway:
    def __init__(self):
        self.puts: list[dict] = []
        self.status: dict = {"state": "connected"}

    async def call(self, method, path, body=None):
        if method == "PUT":
            self.puts.append(body)
            return {}
        return {"status": self.status}


def make(tmp_path, extras):
    db = Database(tmp_path / "t.db")
    db.ex("INSERT INTO hub_membership(id, state, hub_base, hub_domain, hub_name, hub_key, capabilities, api, name, address, mode, hostnames,"
          " tunnel_url, joined_at) VALUES(1,'joined','https://hub.example','hub.example','Hub','{}',?,'x','mara','mara@hub.example','tunnel',?,'wss://t',"
          "'2026-11-18T00:00:00Z')", (json.dumps({"extras": extras}), json.dumps({"instance": "a", "files": "b"})))
    gw = FakeGateway()
    notifier = SimpleNamespace(notify=lambda *a, **k: None)
    live = SimpleNamespace(changed=lambda *a, **k: None, emit=lambda *a, **k: None)
    return Membership(db, notifier, live, gateway=gw, two_factor=lambda: True), gw


def test_settings_push_and_status(tmp_path):
    async def go():
        m, gw = make(tmp_path, [EXTRA])
        await m._set_tunnel()
        assert gw.puts[-1]["config"]["accelerator"] == {"enabled": True, "api": API}
        v = await m.set_accelerator(False)
        assert gw.puts[-1]["config"]["accelerator"] == {"enabled": False, "api": None}
        assert v.accelerator and v.accelerator.offered and not v.accelerator.enabled
        assert v.tunnel and v.tunnel.accelerator and v.tunnel.accelerator.state == "off"  # an older gateway: off
        await m.set_accelerator(True)
        assert gw.puts[-1]["config"]["accelerator"]["enabled"] is True
        gw.status = {"state": "connected", "accelerator": {"state": "error", "since": "2026-11-18T10:00:00Z", "error": "no token"}}
        t = await m.tunnel_status()
        assert t and t.accelerator and t.accelerator.state == "error" and t.accelerator.error == "no token"
        gw.status = {"state": "connected", "accelerator": {"state": "active", "since": "2026-11-18T10:00:00Z", "error": None}}
        assert (await m.tunnel_status()).accelerator.state == "active"

    asyncio.run(go())


def test_not_offered(tmp_path):
    async def go():
        m, gw = make(tmp_path, [])
        await m._set_tunnel()
        assert gw.puts[-1]["config"]["accelerator"] == {"enabled": False, "api": None}
        assert not (await m.view()).accelerator.offered

    asyncio.run(go())
