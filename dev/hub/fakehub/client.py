"""A reference tunnel client (the instance's side of tunnel.md). It connects, signs the hello with a given
key, and forwards each stream to a local HTTP target chosen by the `host` of the OPEN frame.

H3's real tunnel service is modeled on this file. Differences it will have: the signature comes from the
gateway (the tunnel service never holds the key), and the hosts map to control and the files app.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Mapping

import httpx
import websockets
from l0wproto import docs, payloads, tunnel as lt
from l0wproto.keys import KeyPair
from websockets.asyncio.client import connect

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}


class HandshakeRefused(Exception):
    def __init__(self, code: int | None):
        super().__init__(f"hub closed the tunnel handshake with {code}")
        self.code = code


class _Stream:
    def __init__(self):
        self.body: asyncio.Queue = asyncio.Queue()  # bytes | None (END) | Exception (RESET)
        self.window = lt.INITIAL_WINDOW  # what we may still send (response body)
        self.credit = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.reset = False


class TunnelClient:
    def __init__(self, tunnel_url: str, address: str, signer: KeyPair, hub_domain: str, targets: Mapping[str, str], *,
                 release: str = "0.9.0", connect_to: tuple[str, int] | None = None, http: httpx.AsyncClient | None = None,
                 ping_interval: float = 30.0, dead_after: float = 90.0):
        """`targets` maps a hostname (as assigned by the hub) to the base URL its requests are forwarded to."""
        self.url, self.address, self.signer, self.hub_domain = tunnel_url, address, signer, hub_domain
        self.targets = {h.lower(): t.rstrip("/") for h, t in targets.items()}
        self.release, self.connect_to = release, connect_to
        self.http = http or httpx.AsyncClient(timeout=None)
        self._own_http = http is None
        self.ping_interval, self.dead_after = ping_interval, dead_after
        self.ready = asyncio.Event()
        self.session: str | None = None
        self.limits: dict = {}
        self.close_code: int | None = None
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._streams: dict[int, _Stream] = {}

    # --- connection ---------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Reconnect with exponential backoff from 1 s to 60 s, with jitter (tunnel.md)."""
        delay = 1.0
        while True:
            started = time.monotonic()
            try:
                await self.run_once()
            except HandshakeRefused as e:
                if e.code in (4401, 4426):  # the hub does not want us; retrying fast will not change that
                    delay = max(delay, 30.0)
            except (OSError, websockets.ConnectionClosed, asyncio.TimeoutError):
                pass
            if time.monotonic() - started > 60:
                delay = 1.0
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, 60.0)

    async def run_once(self) -> None:
        """One connection: handshake, then serve streams until it closes."""
        kw = {}
        if self.connect_to:
            kw = {"host": self.connect_to[0], "port": self.connect_to[1]}
        async with connect(self.url, subprotocols=[lt.SUBPROTOCOL], max_size=None, ping_interval=None, **kw) as ws:
            self._ws = ws
            self.ready.clear()
            try:
                msg = lt.parse_text_message(await asyncio.wait_for(ws.recv(), 10))
                if msg["t"] != "challenge":
                    raise HandshakeRefused(None)
                hello = docs.sign_document("tunnel-hello", payloads.build_tunnel_hello(
                    self.address, msg["nonce"], msg["hub"], self.release), [self.signer])
                await ws.send(json.dumps(hello))
                ready = lt.parse_text_message(await asyncio.wait_for(ws.recv(), 10))
            except websockets.ConnectionClosed as e:
                self.close_code = e.rcvd.code if e.rcvd else None
                raise HandshakeRefused(self.close_code) from e
            self.session, self.limits = ready["session"], ready["limits"]
            self.ready.set()
            try:
                await self._serve(ws)
            except websockets.ConnectionClosed as e:
                self.close_code = e.rcvd.code if e.rcvd else None
            finally:
                self.ready.clear()
                for s in self._streams.values():
                    s.reset = True
                    s.credit.set()
                    s.body.put_nowait(ConnectionError("tunnel closed"))
                    if s.task:
                        s.task.cancel()
                self._streams.clear()

    async def aclose(self) -> None:
        if self._ws:
            await self._ws.close()
        if self._own_http:
            await self.http.aclose()

    async def _send(self, f: lt.Frame) -> None:
        async with self._send_lock:
            await self._ws.send(lt.encode(f))

    async def _serve(self, ws) -> None:
        last = time.monotonic()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.ping_interval)
            except TimeoutError:
                if time.monotonic() - last > self.dead_after:
                    await ws.close(1001)
                    return
                await self._send(lt.ping_frame(os.urandom(8)))
                continue
            last = time.monotonic()
            if isinstance(raw, str):
                continue
            f = lt.decode(raw)
            if f.type == lt.PING:
                if not f.is_pong:
                    await self._send(lt.pong_for(f))
            elif f.type == lt.OPEN:
                s = _Stream()
                self._streams[f.stream_id] = s
                s.task = asyncio.create_task(self._handle(f.stream_id, s, f.json()))
            else:
                s = self._streams.get(f.stream_id)
                if s is None:
                    continue
                if f.type == lt.DATA:
                    s.body.put_nowait(f.payload)
                elif f.type == lt.END:
                    s.body.put_nowait(None)
                elif f.type == lt.RESET:
                    s.reset = True
                    s.credit.set()
                    s.body.put_nowait(ConnectionError(f"reset by hub: {f.json()['error']}"))
                    if s.task:
                        s.task.cancel()
                elif f.type == lt.CREDIT:
                    s.window += lt.credit_amount(f)
                    s.credit.set()

    # --- one stream ---------------------------------------------------------------------

    def _target(self, host: str) -> str | None:
        host = host.lower()
        if host in self.targets:
            return self.targets[host]
        bare = host.rsplit(":", 1)[0]
        return next((t for h, t in self.targets.items() if h.rsplit(":", 1)[0] == bare), None)

    async def _handle(self, sid: int, s: _Stream, op: dict) -> None:
        try:
            await self._forward(sid, s, op)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass
        except Exception:
            if not s.reset:
                try:
                    await self._send(lt.reset_frame(sid, "upstream_error"))
                except websockets.ConnectionClosed:
                    pass
        finally:
            self._streams.pop(sid, None)

    async def _request_body(self, sid: int, s: _Stream, first: bytes):
        item: bytes | None | Exception = first
        while True:
            if isinstance(item, Exception):
                raise item
            if item is None:
                return
            yield item
            await self._send(lt.credit_frame(sid, len(item)))
            item = await s.body.get()

    async def _forward(self, sid: int, s: _Stream, op: dict) -> None:
        base = self._target(op["host"])
        if base is None:  # tunnel.md: anything that is not ours gets 421
            await self._send(lt.response_frame(sid, 421, [["content-type", "text/plain"]]))
            await self._send(lt.data_frame(sid, b"misdirected request"))
            await self._send(lt.end_frame(sid))
            return
        headers = [(k, v) for k, v in op["headers"] if k not in HOP and k != "host"]
        headers += [("host", op["host"]), ("x-l0w-tunnel-client", op["client"])]
        first = await s.body.get()
        if isinstance(first, Exception):
            raise first
        content = None if first is None else self._request_body(sid, s, first)
        req = self.http.build_request(op["method"], base + op["target"], headers=headers, content=content)
        r = await self.http.send(req, stream=True)
        try:
            out = [[k.lower(), v] for k, v in r.headers.multi_items() if k.lower() not in HOP and not k.lower().startswith("proxy-")]
            await self._send(lt.response_frame(sid, r.status_code, out))
            async for chunk in r.aiter_raw():
                for i in range(0, len(chunk), lt.MAX_DATA):
                    part = chunk[i:i + lt.MAX_DATA]
                    while s.window < len(part):
                        if s.reset:
                            return
                        s.credit.clear()
                        await s.credit.wait()
                    s.window -= len(part)
                    await self._send(lt.data_frame(sid, part))
            await self._send(lt.end_frame(sid))
        finally:
            await r.aclose()


