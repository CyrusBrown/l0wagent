"""The tunnel service: polls the gateway for the hub config, keeps one tunnel connection up, reports its state."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import websockets
from l0wproto.errors import FrameError

from .accelerator import Accelerator
from .client import HandshakeRefused, TunnelClient

log = logging.getLogger("l0w_tunnel")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spec_part(cfg: dict | None) -> dict | None:
    return None if cfg is None else {k: v for k, v in cfg.items() if k != "accelerator"}


class Gateway:
    """The gateway's /v0/tunnel/* endpoints. The tunnel never holds a key."""

    def __init__(self, url: str, token: str, http: httpx.AsyncClient | None = None):
        self.url = url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=10.0, headers={"authorization": f"Bearer {token}"})
        self._own = http is None

    async def config(self) -> dict | None:
        r = await self.http.get(f"{self.url}/v0/tunnel/config")
        r.raise_for_status()
        return r.json().get("config")

    async def hello(self, nonce: str, hub: str) -> dict:
        r = await self.http.post(f"{self.url}/v0/tunnel/hello", json={"nonce": nonce, "hub": hub})
        r.raise_for_status()
        return r.json()["doc"]

    async def status(self, state: str, since: str, error: str | None, session: str | None, close_code: int | None, accelerator: dict | None = None) -> None:
        body = {"state": state, "since": since, "error": error, "session": session, "close_code": close_code}
        if accelerator is not None:
            body["accelerator"] = accelerator
        r = await self.http.put(f"{self.url}/v0/tunnel/status", json=body)
        r.raise_for_status()

    async def accelerator_request(self, action: str, service: str | None) -> dict:
        body = {"action": action}
        if service:
            body["service"] = service
        r = await self.http.post(f"{self.url}/v0/tunnel/accelerator", json=body)
        r.raise_for_status()
        return r.json()["doc"]

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()


class Service:
    def __init__(self, gateway: Gateway, control_url: str, files_url: str, *, poll_seconds: float = 5.0, min_delay: float = 1.0, max_delay: float = 60.0,
                 ping_interval: float = 30.0, dead_after: float = 90.0, accelerator: Accelerator | None = None):
        self.gw, self.control_url, self.files_url = gateway, control_url, files_url
        self.poll, self.min_delay, self.max_delay = poll_seconds, min_delay, max_delay
        self.ping_interval, self.dead_after = ping_interval, dead_after
        self._cfg: dict | None = None
        self._have_cfg = False  # whether the first config poll has succeeded
        self._changed = asyncio.Event()
        self._state: tuple | None = None
        self._spec: tuple | None = None  # the last spec-tunnel state: (state, error, session, close_code)
        self._accel: dict | None = None  # the last accelerator state reported by the accelerator
        self.http = httpx.AsyncClient(timeout=None)
        self.accel = accelerator or Accelerator(self.gw.accelerator_request, self._report_accel, control_url, files_url,
                                                port=int(os.environ.get("L0W_ACCELERATOR_PORT", "8790")), cloudflared=os.environ.get("L0W_CLOUDFLARED", "cloudflared"))

    async def _report_accel(self, accel: dict) -> None:
        self._accel = accel
        if self._spec is not None:  # the status needs a spec state; it carries the accelerator's from then on
            self._state = None
            await self._report(*self._spec)

    async def _report(self, state: str, error: str | None = None, session: str | None = None, close_code: int | None = None) -> None:
        self._spec = (state, error, session, close_code)
        self.accel.spec_connected = state == "connected"
        key = (state, error, session, close_code, self._accel and tuple(self._accel.items()))
        if key == self._state:
            return
        self._state = key
        try:
            await self.gw.status(state, _now(), error, session, close_code, self._accel)
        except Exception as e:  # not fatal: the next transition reports again
            log.warning("could not report state %s to the gateway: %r", state, e)
            self._state = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                cfg = await self.gw.config()
            except Exception as e:
                log.warning("gateway unreachable (config): %r", e)
            else:
                self.accel.configure(cfg)
                if not self._have_cfg or _spec_part(cfg) != _spec_part(self._cfg):  # an accelerator change must not drop the spec tunnel
                    self._have_cfg, self._cfg = True, cfg
                    self._changed.set()
                else:
                    self._cfg = cfg
            await asyncio.sleep(self.poll)

    def _targets(self, cfg: dict) -> dict[str, str]:
        h = cfg["hostnames"]
        return {h["instance"]: self.control_url, h["files"]: self.files_url}

    async def _connect_once(self, cfg: dict, client_box: list) -> None:
        async def sign(nonce: str, hub: str) -> dict:
            return await self.gw.hello(nonce, hub)

        connect_to = None
        if cfg.get("connect_to"):
            host, _, port = cfg["connect_to"].rpartition(":")
            connect_to = (host, int(port))
        client = TunnelClient(cfg["url"], sign, self._targets(cfg), connect_to=connect_to, http=self.http, ping_interval=self.ping_interval, dead_after=self.dead_after)
        client_box.append(client)

        async def announce():
            await client.ready.wait()
            await self._report("connected", session=client.session)
        watcher = asyncio.create_task(announce())
        try:
            await client.run_once()
        finally:
            watcher.cancel()

    async def run(self) -> None:
        poller = asyncio.create_task(self._poll_loop())
        accel = asyncio.create_task(self.accel.run())
        delay = self.min_delay
        try:
            while True:
                if not self._have_cfg:
                    await self._wait_changed(self.poll)
                    continue
                self._changed.clear()
                cfg = self._cfg
                if cfg is None:
                    await self._report("idle")
                    delay = self.min_delay
                    await self._changed.wait()
                    continue
                await self._report("connecting")
                box: list = []
                started = time.monotonic()
                conn = asyncio.create_task(self._connect_once(cfg, box))
                chg = asyncio.create_task(self._changed.wait())
                await asyncio.wait({conn, chg}, return_when=asyncio.FIRST_COMPLETED)
                if not conn.done():  # the config changed or went away: drop this connection
                    conn.cancel()
                    await asyncio.gather(conn, return_exceptions=True)
                    delay = self.min_delay
                    continue
                chg.cancel()
                state, error, code = "offline", None, None
                try:
                    conn.result()
                    code = box[0].close_code if box else None
                except HandshakeRefused as e:
                    code = e.code
                    if code == 4401:
                        state, error, delay = "refused", "handshake refused", self.max_delay
                    elif code == 4426:
                        state, error, delay = "refused", "upgrade required", self.max_delay
                    else:
                        error = str(e)
                except (OSError, websockets.WebSocketException, TimeoutError, FrameError, httpx.HTTPError, KeyError, ValueError) as e:
                    error = f"{type(e).__name__}: {e}"
                    log.warning("tunnel connection failed: %s", error)
                if time.monotonic() - started > 60:
                    delay = self.min_delay
                await self._report(state, error=error, close_code=code)
                await self._wait_changed(delay * random.uniform(0.5, 1.0))
                if not self._changed.is_set():
                    delay = min(delay * 2, self.max_delay)
        finally:
            poller.cancel()
            accel.cancel()
            await asyncio.gather(accel, return_exceptions=True)  # its `finally` stops cloudflared (and tells the hub nothing)

    async def _wait_changed(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except TimeoutError:
            pass

    async def aclose(self) -> None:
        await self.accel.aclose()
        await self.http.aclose()
        await self.gw.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("TUNNEL_GATEWAY_TOKEN")
    if not token:
        raise SystemExit("TUNNEL_GATEWAY_TOKEN is required")
    gw = Gateway(os.environ.get("TUNNEL_GATEWAY_URL", "http://gateway:8780"), token)
    svc = Service(gw, os.environ.get("TUNNEL_CONTROL_URL", "http://control:8000"), os.environ.get("TUNNEL_FILES_URL", "http://control:8001"),
                  poll_seconds=float(os.environ.get("TUNNEL_POLL_SECONDS", "5")))
    log.info("tunnel service starting (gateway %s)", urlsplit(gw.url).netloc)
    async def serve() -> None:
        task = asyncio.create_task(svc.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            await svc.aclose()

    asyncio.run(serve())
