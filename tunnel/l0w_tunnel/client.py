"""One tunnel connection (the instance's side of protocol/tunnel.md): handshake, then forward each stream
to a local HTTP target chosen by the `host` of the OPEN frame.

The tunnel never holds a key: `sign_hello` asks the gateway for the signed tunnel-hello document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

import httpx
import websockets
from l0wproto import tunnel as lt
from websockets.asyncio.client import connect

log = logging.getLogger("l0w_tunnel")

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}
CLIENT_HEADER = "x-l0w-tunnel-client"

SignHello = Callable[[str, str], Awaitable[dict]]  # (nonce, hub) -> signed tunnel-hello document


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


def _hop(name: str) -> bool:
    return name in HOP or name.startswith("proxy-")


class TunnelClient:
    def __init__(self, tunnel_url: str, sign_hello: SignHello, targets: Mapping[str, str], *, connect_to: tuple[str, int] | None = None,
                 http: httpx.AsyncClient | None = None, ping_interval: float = 30.0, dead_after: float = 90.0, handshake_timeout: float = 10.0):
        """`targets` maps a hostname (as assigned by the hub) to the base URL its requests are forwarded to."""
        self.url, self.sign_hello = tunnel_url, sign_hello
        self.targets = {h.lower(): t.rstrip("/") for h, t in targets.items()}
        self.connect_to = connect_to
        self.http = http or httpx.AsyncClient(timeout=None)
        self._own_http = http is None
        self.ping_interval, self.dead_after, self.handshake_timeout = ping_interval, dead_after, handshake_timeout
        self.ready = asyncio.Event()
        self.session: str | None = None
        self.limits: dict = {}
        self.close_code: int | None = None
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._streams: dict[int, _Stream] = {}

    # --- connection ---------------------------------------------------------------------

    async def run_once(self) -> None:
        """One connection: handshake, then serve streams until it closes. Raises HandshakeRefused when the
        hub closes during the handshake; returns (close_code set) when a served connection ends."""
        kw = {}
        if self.connect_to:
            kw = {"host": self.connect_to[0], "port": self.connect_to[1]}
        async with connect(self.url, subprotocols=[lt.SUBPROTOCOL], max_size=None, ping_interval=None, **kw) as ws:
            self._ws = ws
            self.ready.clear()
            try:
                msg = lt.parse_text_message(await asyncio.wait_for(ws.recv(), self.handshake_timeout))
                if msg["t"] != "challenge":
                    raise HandshakeRefused(None)
                hello = await self.sign_hello(msg["nonce"], msg["hub"])
                await ws.send(json.dumps(hello))
                ready = lt.parse_text_message(await asyncio.wait_for(ws.recv(), self.handshake_timeout))
            except websockets.ConnectionClosed as e:
                self.close_code = e.rcvd.code if e.rcvd else None
                raise HandshakeRefused(self.close_code) from e
            if ready["t"] != "ready":
                raise HandshakeRefused(None)
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
        return self.targets.get(host.lower())

    async def _handle(self, sid: int, s: _Stream, op: dict) -> None:
        state = {"head": False}
        try:
            await self._forward(sid, s, op, state)
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass
        except Exception as e:
            log.warning("stream %s: %s %s failed: %r", sid, op.get("method"), op.get("host"), e)
            if not s.reset:
                try:
                    if isinstance(e, httpx.TransportError) and not state["head"]:
                        await self._simple(sid, 502, b"bad gateway")
                    else:
                        await self._send(lt.reset_frame(sid, "upstream_error"))
                except websockets.ConnectionClosed:
                    pass
        finally:
            self._streams.pop(sid, None)

    async def _simple(self, sid: int, status: int, body: bytes) -> None:
        await self._send(lt.response_frame(sid, status, [["content-type", "text/plain"], ["content-length", str(len(body))]]))
        await self._send(lt.data_frame(sid, body))
        await self._send(lt.end_frame(sid))

    async def _request_body(self, sid: int, s: _Stream, first: bytes) -> AsyncIterator[bytes]:
        item: bytes | None | Exception = first
        while True:
            if isinstance(item, Exception):
                raise item
            if item is None:
                return
            yield item
            await self._send(lt.credit_frame(sid, len(item)))
            item = await s.body.get()

    async def _forward(self, sid: int, s: _Stream, op: dict, state: dict) -> None:
        base = self._target(op["host"])
        if base is None:  # tunnel.md: anything that is not ours gets 421, never forwarded
            await self._simple(sid, 421, b"misdirected request")
            return
        headers = [(k, v) for k, v in op["headers"] if not _hop(k) and k != "host" and not k.startswith("x-l0w-tunnel-")]
        headers += [("host", op["host"]), (CLIENT_HEADER, op["client"])]
        first = await s.body.get()
        if isinstance(first, Exception):
            raise first
        content = None if first is None else self._request_body(sid, s, first)
        req = self.http.build_request(op["method"], base + op["target"], headers=headers, content=content)
        r = await self.http.send(req, stream=True)
        try:
            out = [[k.lower(), v] for k, v in r.headers.multi_items() if not _hop(k.lower())]
            await self._send(lt.response_frame(sid, r.status_code, out))
            state["head"] = True
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
