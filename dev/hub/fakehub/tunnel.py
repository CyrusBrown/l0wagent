"""The tunnel server (tunnel.md): the WebSocket handshake, the frames with flow control, and the front
that sends any request for a member's hostname down that member's tunnel."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque

from l0wproto import VERSION, b64, docs, payloads, tunnel as lt
from l0wproto.errors import FrameError
from l0wproto.keys import Key
from starlette.websockets import WebSocket

from .config import Config

HOP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"}


class Aborted:
    """Put on a stream's body queue when the instance RESET it after the response began."""


class Stream:
    def __init__(self, sid: int):
        self.id = sid
        self.head: asyncio.Future = asyncio.get_running_loop().create_future()  # ("response", obj) | ("offline",) | ("too_large",)
        self.body: asyncio.Queue = asyncio.Queue()  # bytes | None (END) | Aborted
        self.send_window = lt.INITIAL_WINDOW  # what the hub may still send (request body)
        self.window_event = asyncio.Event()
        self.recv_credit = lt.INITIAL_WINDOW  # what the instance may still send (response body)
        self.reset = False
        self.gone = asyncio.Event()  # the visitor went away
        self.request_ended = False
        self.response_ended = False

    def resolve(self, value) -> None:
        if not self.head.done():
            self.head.set_result(value)

    def abort(self) -> None:
        """The stream ends without a normal END (RESET, or the connection closed)."""
        self.reset = True
        self.window_event.set()
        if not self.head.done():
            self.head.set_result(("offline",))
        else:
            self.body.put_nowait(Aborted())


class Session:
    def __init__(self, server: TunnelServer, name: str, ws: WebSocket, session_id: str):
        self.server, self.name, self.ws, self.id = server, name, ws, session_id
        self.streams: dict[int, Stream] = {}
        self.next_id = 1
        self.draining = False
        self.closed = False
        self._lock = asyncio.Lock()
        self.last_recv = time.monotonic()

    @property
    def usable(self) -> bool:
        return not self.draining and not self.closed

    async def send_frame(self, f: lt.Frame) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_bytes(lt.encode(f))
            except Exception:
                self.closed = True

    async def close(self, code: int) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.ws.close(code=code)
            except Exception:
                pass
        self.fail_streams()

    def fail_streams(self) -> None:
        for s in list(self.streams.values()):
            s.abort()

    async def drain_then_close(self) -> None:
        """A replaced connection: no new streams; closed 4409 once its streams end (at most drain_seconds)."""
        self.draining = True
        deadline = time.monotonic() + self.server.cfg.drain_seconds
        while self.streams and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await self.close(lt.CLOSE_REPLACED)

    # --- reading ------------------------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.server.cfg
        try:
            while not self.closed:
                try:
                    msg = await asyncio.wait_for(self.ws.receive(), timeout=cfg.ping_interval)
                except TimeoutError:
                    if time.monotonic() - self.last_recv > cfg.dead_after:
                        await self.close(1001)
                        break
                    await self.send_frame(lt.ping_frame(os.urandom(8)))
                    continue
                self.last_recv = time.monotonic()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is None:
                    await self.close(1003)  # text after ready is not part of the protocol
                    break
                try:
                    frame = lt.decode(msg["bytes"])
                except FrameError:
                    await self.close(1002)
                    break
                await self.handle(frame)
        finally:
            self.closed = True
            self.fail_streams()

    async def handle(self, f: lt.Frame) -> None:
        if f.type == lt.PING:
            if not f.is_pong:
                await self.send_frame(lt.pong_for(f))
            return
        s = self.streams.get(f.stream_id)
        if s is None:
            return  # a stream we already finished; late frames are ignored
        if f.type == lt.RESPONSE:
            s.resolve(("response", f.json()))
        elif f.type == lt.DATA:
            s.recv_credit -= len(f.payload)
            if s.recv_credit < 0:
                await self.send_frame(lt.reset_frame(s.id, "flow_control"))
                s.abort()
            else:
                s.body.put_nowait(f.payload)
        elif f.type == lt.END:
            s.response_ended = True
            s.body.put_nowait(None)
        elif f.type == lt.RESET:
            s.abort()
        elif f.type == lt.CREDIT:
            s.send_window += lt.credit_amount(f)
            s.window_event.set()


def prefers_json(accept: str) -> bool:
    best = {"json": (-1.0, 0), "html": (-1.0, 0)}
    for i, part in enumerate(accept.split(",")):
        bits = [b.strip() for b in part.split(";")]
        mt, q = bits[0].lower(), 1.0
        for b in bits[1:]:
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        kind = "json" if mt in ("application/json", "application/*") else "html" if mt in ("text/html", "application/xhtml+xml") else None
        if kind and (q, -i) > best[kind]:
            best[kind] = (q, -i)
    return best["json"] > best["html"]


class TunnelServer:
    def __init__(self, hub):
        self.hub = hub
        self.sessions: dict[str, Session] = {}
        self._auth_hits: dict[str, deque] = {}

    @property
    def cfg(self) -> Config:
        return self.hub.cfg

    def asgi(self, app):
        return Front(self, app)

    def lookup(self, host: str):
        hm = self.hub.host_map()
        if host in hm:
            return hm[host]
        bare = host.rsplit(":", 1)[0] if ":" in host else host
        for h, v in hm.items():
            if h.rsplit(":", 1)[0] == bare:
                return v
        return None

    async def drop(self, name: str, code: int) -> None:
        s = self.sessions.pop(name, None)
        if s:
            await s.close(code)

    def auth_allowed(self, name: str) -> bool:
        """A per-member sliding minute on /api/auth/*."""
        limit = self.cfg.limits["auth_rate_per_minute"]
        q = self._auth_hits.setdefault(name, deque())
        t = time.monotonic()
        while q and t - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True

    # --- the tunnel endpoint --------------------------------------------------------------

    async def handle_tunnel(self, scope, receive, send) -> None:
        ws = WebSocket(scope, receive, send)
        if lt.SUBPROTOCOL not in scope.get("subprotocols", []):
            await ws.receive()  # websocket.connect
            await ws.close(code=1002)  # before accept: refused
            return
        await ws.accept(subprotocol=lt.SUBPROTOCOL)
        nonce = os.urandom(32)
        sent_at = time.monotonic()
        await ws.send_text(lt.challenge_message(nonce, self.cfg.domain))

        async def refuse(code: int = 4401):
            try:
                await ws.close(code=code)
            except Exception:
                pass

        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=self.cfg.handshake_timeout)
            if msg["type"] != "websocket.receive" or msg.get("text") is None:
                return await refuse()
            doc = json.loads(msg["text"])
            p = docs.parse_payload(doc)
            name, domain = payloads.split_address(p["address"])
            if domain != self.cfg.domain.lower():
                return await refuse()
            m = self.hub.store.member(name)
            if not m or m["status"] != "active" or m["mode"] != "tunnel":
                return await refuse()
            payloads.verify_tunnel_hello(doc, Key.from_dict(m["keys"]["sign"]), nonce=b64.encode(nonce), hub=self.cfg.domain)
            if time.monotonic() - sent_at > 30:
                return await refuse()
            from .hub import release_tuple

            if release_tuple(p["release"]) < release_tuple(self.cfg.min_release):
                return await refuse(lt.CLOSE_UPGRADE_REQUIRED)
        except Exception:
            return await refuse()

        sid = b64.encode(os.urandom(9))
        await ws.send_text(lt.ready_message(sid, self.cfg.limits))
        session = Session(self, name, ws, sid)
        old = self.sessions.get(name)
        self.sessions[name] = session
        if old:
            asyncio.create_task(old.drain_then_close())
        try:
            await session.run()
        finally:
            if self.sessions.get(name) is session:
                del self.sessions[name]
            session.closed = True
            session.fail_streams()

    # --- visitors -------------------------------------------------------------------------

    async def respond(self, send, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        hs = [(k.encode(), v.encode()) for k, v in headers] + [(b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": status, "headers": hs})
        await send({"type": "http.response.body", "body": body})

    async def error(self, send, status: int, code: str, detail: str, extra=()) -> None:
        body = json.dumps({"v": VERSION, "error": code, "detail": detail}).encode()
        await self.respond(send, status, [("content-type", "application/json"), ("cache-control", "no-store"), *extra], body)

    async def offline(self, send, accept: str) -> None:
        extra = [("retry-after", "30"), ("cache-control", "no-store")]
        if prefers_json(accept):
            body = json.dumps({"v": VERSION, "error": "instance_offline"}).encode()
            await self.respond(send, 502, [("content-type", "application/json"), *extra], body)
        else:
            html = b"<!doctype html><title>Offline</title><h1>This instance is offline</h1><p>Try again in a little while.</p>"
            await self.respond(send, 502, [("content-type", "text/html; charset=utf-8"), *extra], html)

    async def proxy(self, scope, receive, send, name: str, kind: str, host: str) -> None:
        hdrs = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope["headers"]]
        h = dict(hdrs)
        limits = self.cfg.limits
        path = scope["path"]
        if (h.get("upgrade", "").lower() == "websocket") or scope["type"] == "websocket":
            return await self.reject_upgrade(scope, receive, send)
        if path.startswith("/api/auth/") and not self.auth_allowed(name):
            return await self.error(send, 429, "rate_limited", "too many attempts", [("retry-after", "60")])
        try:
            declared = int(h.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limits["request_body_bytes"]:
            return await self.error(send, 413, "request_too_large", "request body over the hub's limit")
        session = self.sessions.get(name)
        if session is None or not session.usable:
            return await self.offline(send, h.get("accept", ""))
        if len(session.streams) >= limits["tunnel_streams"]:
            return await self.error(send, 503, "too_many_streams", "too many open streams", [("retry-after", "1")])

        member = self.hub.store.member(name)
        host = member["hostnames"][kind] if member else host  # exactly what the hub assigned
        sid = session.next_id
        session.next_id += 2
        stream = Stream(sid)
        session.streams[sid] = stream
        target = (scope.get("raw_path") or path.encode()).decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        client = (scope.get("client") or ("0.0.0.0", 0))[0]
        pump: asyncio.Task | None = None
        try:
            await session.send_frame(lt.open_frame(
                sid, scope["method"], target, host, lt.filter_request_headers(hdrs), client, scope.get("scheme", "https")))
            pump = asyncio.create_task(self._pump_request(session, stream, receive, limits["request_body_bytes"]))
            waiter = asyncio.create_task(stream.gone.wait())
            await asyncio.wait([stream.head, waiter], return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if not stream.head.done():
                return  # the visitor left before any answer
            result = stream.head.result()
            if result[0] == "offline":
                return await self.offline(send, h.get("accept", ""))
            if result[0] == "too_large":
                return await self.error(send, 413, "request_too_large", "request body over the hub's limit")
            await self._stream_response(session, stream, result[1], send)
        finally:
            if pump:
                pump.cancel()
            if not (stream.request_ended and stream.response_ended) and not stream.reset and not session.closed:
                await session.send_frame(lt.reset_frame(sid, "cancelled"))
            session.streams.pop(sid, None)

    async def _pump_request(self, session: Session, stream: Stream, receive, limit: int) -> None:
        total = 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                stream.gone.set()
                return
            chunk = msg.get("body", b"")
            total += len(chunk)
            if total > limit:
                await session.send_frame(lt.reset_frame(stream.id, "request_too_large"))
                stream.reset = True
                stream.resolve(("too_large",))
                return
            off = 0
            while off < len(chunk):
                while stream.send_window <= 0:
                    if stream.reset or session.closed:
                        return
                    stream.window_event.clear()
                    await stream.window_event.wait()
                if stream.reset:
                    return
                n = min(lt.MAX_DATA, len(chunk) - off, stream.send_window)
                stream.send_window -= n
                await session.send_frame(lt.data_frame(stream.id, chunk[off:off + n]))
                off += n
            if not msg.get("more_body"):
                await session.send_frame(lt.end_frame(stream.id))
                stream.request_ended = True
                break
        while True:  # the body is in; keep listening so a departing visitor is noticed
            msg = await receive()
            if msg["type"] == "http.disconnect":
                stream.gone.set()
                return

    async def _stream_response(self, session: Session, stream: Stream, head: dict, send) -> None:
        headers = [(k.encode(), v.encode()) for k, v in head["headers"]
                   if k not in HOP_RESPONSE and not k.startswith("proxy-")]
        await send({"type": "http.response.start", "status": head["status"], "headers": headers})
        owed = 0
        while True:
            getter = asyncio.create_task(stream.body.get())
            waiter = asyncio.create_task(stream.gone.wait())
            await asyncio.wait([getter, waiter], return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            if not getter.done():
                getter.cancel()
                return
            item = getter.result()
            if isinstance(item, Aborted):
                return  # cut the visitor's connection: the body is incomplete
            if item is None:
                break
            await send({"type": "http.response.body", "body": item, "more_body": True})
            owed += len(item)
            if owed >= 16384 and not stream.reset:
                stream.recv_credit += owed
                await session.send_frame(lt.credit_frame(stream.id, owed))
                owed = 0
        await send({"type": "http.response.body", "body": b""})

    async def reject_upgrade(self, scope, receive, send) -> None:
        body = json.dumps({"v": VERSION, "error": "websocket_unsupported",
                           "detail": "WebSocket upgrades through the tunnel are not part of l0w/0.1"}).encode()
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            if "websocket.http.response" in scope.get("extensions", {}):
                await send({"type": "websocket.http.response.start", "status": 501,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "websocket.http.response.body", "body": body})
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.respond(send, 501, [("content-type", "application/json")], body)


class Front:
    """Host-based routing: a member's hostname goes down the tunnel, anything else is the hub API."""

    def __init__(self, server: TunnelServer, app):
        self.server, self.app = server, app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            host = next((v.decode("latin-1").lower() for k, v in scope["headers"] if k == b"host"), "")
            hit = self.server.lookup(host)
            if hit:
                name, kind = hit
                return await self.server.proxy(scope, receive, send, name, kind, host)
            if scope["type"] == "websocket" and scope["path"] == "/v0/tunnel":
                return await self.server.handle_tunnel(scope, receive, send)
        await self.app(scope, receive, send)
