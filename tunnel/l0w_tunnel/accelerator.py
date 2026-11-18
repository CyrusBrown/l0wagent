"""The `cloudflared` accelerator (protocol/accelerators.md), instance side.

While the spec tunnel stays connected, this module: asks the hub (with gateway-signed accelerator-requests) for a
Cloudflare Tunnel, runs `cloudflared` with its token, serves what comes out of it on a loopback listener (the same Host
routing as the spec tunnel), and asks the hub to move the hostnames onto it once it has been ready for a while, and back
when it has not. The token lives in this process's memory and in `cloudflared`'s environment; it is never logged.

On shutdown (SIGTERM) only `cloudflared` is stopped: the hub is not told to deactivate or disable, because the instance
may just be restarting. Visitors then see Cloudflare's error page until the instance is back and the next start
re-activates (or the hub's operator moves the hostnames back). That brief window is accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

import httpx
import uvicorn

from .client import CLIENT_HEADER, _hop

log = logging.getLogger("l0w_tunnel.accelerator")

SERVICE_HOST = "127.0.0.1"
MIRROR_HEADER = "cf-connecting-ip"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AccelError(Exception):
    """A request to the hub failed. `code` is the hub's error code (or "network"), `status` the HTTP status (0 for none)."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{status} {code} {detail}".strip())
        self.status, self.code = status, code


# --- the local listener --------------------------------------------------------------------


def make_proxy_app(targets: Mapping[str, str] | Callable[[], Mapping[str, str]], http: httpx.AsyncClient):
    """An ASGI app that hands a request to the target for its Host (anything else is 421), as the spec tunnel client does.
    The visitor's address comes only from CF-Connecting-IP; every incoming `x-l0w-tunnel-*` header is dropped."""

    async def reply(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                m = await receive()
                if m["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":  # a websocket scope: refuse (uvicorn answers 403)
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        raw = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope["headers"]]
        host = next((v for k, v in raw if k == "host"), "")
        table = targets() if callable(targets) else targets
        base = next((t.rstrip("/") for h, t in table.items() if h.lower() == host.lower()), None)
        if base is None:
            return await reply(send, 421, b"misdirected request")
        if any(k == "upgrade" for k, _ in raw):  # WebSocket and other upgrades are not forwarded
            return await reply(send, 501, b"upgrades are not supported")
        client = next((v for k, v in raw if k == MIRROR_HEADER), "").strip()
        if not client:
            return await reply(send, 400, b"missing client address")
        headers = [(k, v) for k, v in raw if not _hop(k) and k != "host" and k != MIRROR_HEADER and not k.startswith("x-l0w-tunnel-")]
        headers += [("host", host), (CLIENT_HEADER, client)]

        async def body():
            while True:
                m = await receive()
                if m["type"] == "http.disconnect":
                    raise ConnectionError("client went away")
                if m.get("body"):
                    yield m["body"]
                if not m.get("more_body"):
                    return

        first = await receive()
        if first["type"] == "http.disconnect":
            return
        if not first.get("body") and not first.get("more_body"):
            content = None
        else:
            async def content_gen():
                if first.get("body"):
                    yield first["body"]
                if first.get("more_body"):
                    async for part in body():
                        yield part
            content = content_gen()
        path = scope["raw_path"].decode("latin-1") if scope.get("raw_path") else scope["path"]
        if scope.get("query_string"):
            path += "?" + scope["query_string"].decode("latin-1")
        started = False
        try:
            req = http.build_request(scope["method"], base + path, headers=headers, content=content)
            r = await http.send(req, stream=True)
            try:
                out = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in r.headers.multi_items() if not _hop(k.lower())]
                await send({"type": "http.response.start", "status": r.status_code, "headers": out})
                started = True
                async for chunk in r.aiter_raw():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
            finally:
                await r.aclose()
        except Exception as e:
            log.warning("accelerator: %s %s failed: %r", scope["method"], host, e)
            if not started:
                await reply(send, 502, b"bad gateway")
            else:
                raise

    return app


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # the service owns SIGTERM/SIGINT, not the listener
        yield


# --- the manager ---------------------------------------------------------------------------


class Accelerator:
    def __init__(self, request: Callable[[str, str | None], Awaitable[dict]], report: Callable[[dict], Awaitable[None]], control_url: str, files_url: str, *,
                 port: int = 8790, metrics: str = "127.0.0.1:20241", cloudflared: str = "cloudflared", tick: float = 2.0, ready_after: float = 10.0,
                 down_after: float = 30.0, min_delay: float = 1.0, max_delay: float = 60.0, stale_delay: float = 1.0, http: httpx.AsyncClient | None = None):
        self.request, self.report = request, report  # request(action, service) -> signed doc (gateway); report({"state","since","error"})
        self.control_url, self.files_url = control_url, files_url
        self.port, self.metrics, self.cloudflared = port, metrics, cloudflared
        self.tick, self.ready_after, self.down_after = tick, ready_after, down_after
        self.min_delay, self.max_delay, self.stale_delay = min_delay, max_delay, stale_delay
        self.http = http or httpx.AsyncClient(timeout=15.0)
        self._own_http = http is None
        self.probe = httpx.AsyncClient(timeout=2.0)
        self.proxy_http = httpx.AsyncClient(timeout=None)
        self._cfg: dict | None = None  # the whole tunnel config (None: none)
        self.spec_connected = False
        self.state = "off"
        self._token: str | None = None
        self._enabled = False  # the hub has a tunnel for us (enable succeeded since the last disable)
        self.active = False
        self._proc: asyncio.subprocess.Process | None = None
        self._server: _Server | None = None
        self._server_task: asyncio.Task | None = None
        self._ready_since: float | None = None
        self._down_since: float | None = None
        self._retry_at = 0.0
        self._delay = min_delay
        self._proc_retry_at = 0.0
        self._proc_delay = min_delay
        self._proc_started = 0.0
        self._key: tuple | None = None
        self._since = _now()

    # --- inputs ---

    def configure(self, cfg: dict | None) -> None:
        self._cfg = cfg

    def _wanted(self) -> dict | None:
        a = (self._cfg or {}).get("accelerator") or {}
        return a if a.get("enabled") and a.get("api") else None

    @property
    def service(self) -> str:
        return f"http://{SERVICE_HOST}:{self.port}"

    # --- reporting ---

    async def _set(self, state: str, error: str | None = None) -> None:
        if self._key == (state, error):
            return
        if state != self.state:
            self._since = _now()
        self.state, self._key = state, (state, error)
        try:
            await self.report({"state": state, "since": self._since, "error": error})
        except Exception as e:
            log.warning("could not report accelerator state %s: %r", state, e)
            self._key = None

    # --- the hub's API ---

    async def _post(self, action: str, api: str) -> dict:
        """One accelerator-request; returns the hub's `state`. 409 stale is signed again once, after a pause."""
        for attempt in (0, 1):
            try:
                doc = await self.request(action, self.service if action == "enable" else None)
                r = await self.http.post(api, json=doc)
            except httpx.HTTPError as e:
                raise AccelError(0, "network", type(e).__name__) from e
            if r.status_code == 200:
                return r.json()["state"]
            code = ""
            with contextlib.suppress(ValueError, AttributeError):
                code = str(r.json().get("error", ""))
            if r.status_code == 409 and code == "stale" and attempt == 0:
                await asyncio.sleep(self.stale_delay)
                continue
            raise AccelError(r.status_code, code or "error")
        raise AccelError(409, "stale")

    async def _call(self, action: str, api: str) -> dict | None:
        """Runs one action and keeps the retry bookkeeping. Returns the state, or None when it failed (retried later)."""
        try:
            st = await self._post(action, api)
        except AccelError as e:
            if e.code == "not_connected":  # cloudflared is not connected yet from the hub's view: ask again later
                self._retry_at = time.monotonic() + max(self.tick, 2.0)
                log.info("accelerator %s: hub says not connected yet", action)
                return None
            if e.code == "not_enabled" and action != "enable":
                self._enabled, self._token = False, None  # the hub lost it: enable again
            log.warning("accelerator %s failed: %s", action, e)
            self._retry_at = time.monotonic() + self._delay
            self._delay = min(self._delay * 2, self.max_delay)
            await self._set("error", f"{action}: {e.code or e.status}")
            return None
        self._delay = self.min_delay
        return st

    # --- the listener and cloudflared ---

    async def _ensure_listener(self) -> None:
        if self._server_task and not self._server_task.done():
            return
        app = make_proxy_app(self._targets, self.proxy_http)
        cfg = uvicorn.Config(app, host=SERVICE_HOST, port=self.port, log_level="warning", access_log=False, ws="none", lifespan="off", server_header=False)
        self._server = _Server(cfg)
        self._server_task = asyncio.create_task(self._server.serve())

    def _targets(self) -> dict[str, str]:
        h = (self._cfg or {}).get("hostnames") or {}
        return {h[k]: u for k, u in (("instance", self.control_url), ("files", self.files_url)) if k in h}

    async def _stop_listener(self) -> None:
        if self._server and self._server_task:
            self._server.should_exit = True
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._server_task, 5)
            if not self._server_task.done():
                self._server_task.cancel()
        self._server = self._server_task = None

    async def _start_proc(self) -> None:
        env = {"TUNNEL_TOKEN": self._token or ""}  # nothing else of ours (the gateway token) goes to cloudflared
        for k in ("PATH", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR"):
            if k in os.environ:
                env[k] = os.environ[k]
        self._proc = await asyncio.create_subprocess_exec(self.cloudflared, "tunnel", "--no-autoupdate", "--metrics", self.metrics, "run", env=env)
        self._proc_started = time.monotonic()
        log.info("cloudflared started (pid %s)", self._proc.pid)

    async def _stop_proc(self) -> None:
        p, self._proc = self._proc, None
        if p is None or p.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()
        log.info("cloudflared stopped")

    async def _ready(self) -> bool:
        if self._proc is None or self._proc.returncode is not None:
            return False
        try:
            return (await self.probe.get(f"http://{self.metrics}/ready")).status_code == 200
        except httpx.HTTPError:
            return False

    # --- the loop ---

    async def _tick(self) -> None:
        now = time.monotonic()
        want = self._wanted()
        if want is None:
            await self._turn_off()
            return
        api = want["api"]
        if not self._enabled and not self.spec_connected:
            return  # wait for the spec tunnel (the fallback) to be up before asking for anything
        if now < self._retry_at:
            return
        if not self._enabled:
            st = await self._call("enable", api)
            if st is None:
                return
            self._token = st.get("token") or self._token
            if not self._token:
                self._retry_at = now + self._delay
                await self._set("error", "enable: the hub gave no token")
                return
            self._enabled, self.active = True, bool(st.get("active"))
            await self._ensure_listener()
            await self._set("active" if self.active else "starting")
        await self._ensure_listener()
        if (self._proc is None or self._proc.returncode is not None) and now >= self._proc_retry_at:
            if self._proc is not None:  # it exited: back off before the next start
                log.warning("cloudflared exited with %s", self._proc.returncode)
                self._proc_delay = self.min_delay if now - self._proc_started > 60 else min(self._proc_delay * 2, self.max_delay)
                self._proc_retry_at = now + self._proc_delay
                self._proc = None
            else:
                try:
                    await self._start_proc()
                except OSError as e:
                    log.warning("cloudflared could not start: %r", e)
                    self._proc_retry_at = now + self._proc_delay
                    self._proc_delay = min(self._proc_delay * 2, self.max_delay)
                    await self._set("error", "cloudflared could not start")
                    return
        ready = await self._ready()
        if ready:
            self._down_since = None
            self._ready_since = self._ready_since or now
            if not self.active:
                if self.spec_connected and now - self._ready_since >= self.ready_after:
                    st = await self._call("activate", api)
                    if st is not None:
                        self.active = True
                        await self._set("active")
                    return
                await self._set("ready" if now - self._ready_since >= self.ready_after else "starting")
            else:
                await self._set("active")
        else:
            self._ready_since = None
            self._down_since = self._down_since or now
            if self.active:
                if self.spec_connected and now - self._down_since >= self.down_after:
                    st = await self._call("deactivate", api)
                    if st is not None:
                        self.active = False
                        await self._set("starting")
                    return
                await self._set("active")  # still pointed at the tunnel; the fallback follows once it has been down long enough
            elif self.state not in ("error",) or self._retry_at <= now:
                await self._set("starting")

    async def _turn_off(self) -> None:
        if self._enabled:
            if self._cfg is not None and (self._cfg.get("accelerator") or {}).get("api"):
                if time.monotonic() < self._retry_at:
                    return
                if await self._call("disable", self._cfg["accelerator"]["api"]) is None:
                    return  # retried; cloudflared stays up until the hub has let go
            self._enabled = self.active = False
        self._token = None
        await self._stop_proc()
        await self._stop_listener()
        self._ready_since = self._down_since = None
        self._retry_at, self._delay = 0.0, self.min_delay
        await self._set("off")

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("accelerator tick failed")
                await asyncio.sleep(self.tick)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shutdown: stop cloudflared and the listener. The hub is NOT told (see the module docstring)."""
        await self._stop_proc()
        await self._stop_listener()

    async def aclose(self) -> None:
        await self.stop()
        await self.probe.aclose()
        await self.proxy_http.aclose()
        if self._own_http:
            await self.http.aclose()
