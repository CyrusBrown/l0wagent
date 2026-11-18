"""Plumbing for the suite: results, networking (optionally to a fixed address), a fake instance's keys
and documents, and a raw tunnel peer."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import ssl
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import websockets
from l0wproto import VERSION, docs, keys, payloads, tunnel as lt
from l0wproto.times import format_time, now
from websockets.asyncio.client import connect as ws_connect


class Failure(AssertionError):
    pass


def expect(cond, msg: str = "check failed") -> None:
    if not cond:
        raise Failure(msg)


class Report:
    def __init__(self, verbose: bool = False):
        self.results: list[tuple[str, str, str]] = []
        self.verbose = verbose

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.results.append((status, name, detail))
        line = f"{status:<5} {name}" + (f": {detail}" if detail and (status != "PASS" or self.verbose) else "")
        print(line, flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.results if s == "FAIL")

    def summary(self) -> str:
        c = {k: sum(1 for s, _, _ in self.results if s == k) for k in ("PASS", "FAIL", "SKIP", "WARN")}
        return f"{c['PASS']} passed, {c['FAIL']} failed, {c['SKIP']} skipped, {c['WARN']} warnings"

    async def step(self, name: str, fn, *args, timeout: float = 60.0):
        """Run one check. Returns its value, or None after recording a FAIL."""
        try:
            val = await asyncio.wait_for(fn(*args), timeout)
        except Failure as e:
            self.add("FAIL", name, str(e))
        except TimeoutError:
            self.add("FAIL", name, f"timed out after {timeout:g}s")
        except Exception as e:
            self.add("FAIL", name, f"{type(e).__name__}: {e}")
        else:
            self.add("PASS", name)
            return val if val is not None else True
        return None


class _Redirect(httpx.AsyncBaseTransport):
    """Send every request to a fixed host:port, keeping the Host header and TLS name."""

    def __init__(self, inner: httpx.AsyncBaseTransport, host: str, port: int):
        self.inner, self.host, self.port = inner, host, port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ext = dict(request.extensions)
        ext["sni_hostname"] = request.url.host
        new = httpx.Request(request.method, request.url.copy_with(host=self.host, port=self.port), headers=request.headers,
                            stream=request.stream, extensions=ext)
        return await self.inner.handle_async_request(new)

    async def aclose(self):
        await self.inner.aclose()


class Net:
    def __init__(self, hub_url: str, connect: str | None = None, insecure: bool = False):
        u = urlsplit(hub_url)
        self.base = f"{u.scheme}://{u.netloc}"
        self.scheme = u.scheme
        self.insecure = insecure
        self.connect_to: tuple[str, int] | None = None
        if connect:
            h, _, p = connect.rpartition(":")
            self.connect_to = (h, int(p))
        inner = httpx.AsyncHTTPTransport(verify=not insecure)
        transport = _Redirect(inner, *self.connect_to) if self.connect_to else inner
        self.http = httpx.AsyncClient(transport=transport, timeout=20.0, follow_redirects=False)

    async def close(self):
        await self.http.aclose()

    def url_for_host(self, hostname: str, path: str = "/") -> str:
        return f"{self.scheme}://{hostname}{path}"

    async def ws(self, url: str, **kw):
        extra = {"host": self.connect_to[0], "port": self.connect_to[1]} if self.connect_to else {}
        if url.startswith("wss://") and self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            extra["ssl"] = ctx
        return await ws_connect(url, max_size=None, ping_interval=None, open_timeout=10, **extra, **kw)

    async def raw(self, hostname: str, data: bytes, read_timeout: float = 5.0) -> bytes:
        """Send raw bytes to a hostname, return what comes back until the first response head is complete."""
        host, _, port = hostname.partition(":")
        if self.connect_to:
            host, port = self.connect_to[0], str(self.connect_to[1])
        tls = None
        if self.scheme == "https":
            tls = ssl.create_default_context()
            if self.insecure:
                tls.check_hostname, tls.verify_mode = False, ssl.CERT_NONE
        r, w = await asyncio.open_connection(host, int(port or (443 if tls else 80)), ssl=tls, server_hostname=hostname.partition(":")[0] if tls else None)
        try:
            w.write(data)
            await w.drain()
            buf = b""
            async with asyncio.timeout(read_timeout):
                while b"\r\n\r\n" not in buf:
                    chunk = await r.read(4096)
                    if not chunk:
                        break
                    buf += chunk
            return buf
        finally:
            w.close()


def expect_error(r: httpx.Response, status: int, code: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    expect(r.status_code == status and isinstance(body, dict) and body.get("error") == code,
           f"expected {status} {code}, got {r.status_code} {r.text[:200]!r}")
    return body


@dataclass
class Inst:
    """A fake instance: its own keys, and the documents it signs."""

    domain: str
    name: str
    sign: keys.KeyPair = field(default_factory=lambda: keys.generate("sign"))
    seal: keys.KeyPair = field(default_factory=lambda: keys.generate("seal"))
    recover: keys.KeyPair = field(default_factory=lambda: keys.generate("recover"))
    hostnames: dict | None = None
    tunnel_url: str | None = None
    history: list = field(default_factory=list)

    @property
    def address(self) -> str:
        return f"{self.name}@{self.domain}"

    @property
    def key_objs(self) -> list:
        return [self.sign.key, self.seal.key, self.recover.key]

    def registration(self, invite: str, *, mode="tunnel", hub=None, iat=None, release="0.9.0", signers=None,
                     name=None, own_urls=None, v=VERSION) -> dict:
        p = payloads.build_registration(hub or self.domain, name or self.name, invite, mode, self.key_objs, own_urls=own_urls,
                                        release=release, iat=iat)
        if v != VERSION:
            p["v"] = v
        return docs.sign_document("registration", p, signers or [self.sign, self.recover], v=v)

    def self_doc(self, seq: int, *, keys_=None, history=None, hostnames=None, signer=None, address=None) -> dict:
        p = payloads.build_self(address or self.address, seq, keys_ or self.key_objs, hostnames or self.hostnames,
                                history=self.history if history is None else history)
        return docs.sign_document("self", p, [signer or self.sign])

    def rotate_sign(self, *, signer=None, reason="scheduled") -> tuple[keys.KeyPair, dict]:
        """A rotation document for the signing key (not yet applied to this object)."""
        new = keys.generate("sign")
        p = payloads.build_rotation(self.address, self.sign.kid, new.key, reason)
        return new, docs.sign_document("rotation", p, [signer or self.sign])

    def leave_doc(self, *, signer=None, address=None) -> dict:
        return docs.sign_document("leave", payloads.build_leave(address or self.address), [signer or self.sign])

    def hello(self, challenge: dict, *, nonce=None, hub=None, iat=None, signer=None, address=None) -> str:
        import json

        p = payloads.build_tunnel_hello(address or self.address, nonce or challenge["nonce"], hub or challenge["hub"], iat=iat)
        return json.dumps(docs.sign_document("tunnel-hello", p, [signer or self.sign]))

    def accel(self, action: str, service: str | None = None, *, iat=None, hub=None, signer=None, address=None,
              accelerator="cloudflared") -> dict:
        """A signed accelerator-request (accelerators.md); `service=None` leaves the field out."""
        p = payloads.build_accelerator_request(address or self.address, hub or self.domain, action,
                                               payloads._OMIT if service is None else service, accelerator=accelerator, iat=iat)
        return docs.sign_document("accelerator-request", p, [signer or self.sign])


class Closed(Exception):
    def __init__(self, code):
        super().__init__(f"tunnel closed ({code})")
        self.code = code


class Peer:
    """The instance end of a tunnel, speaking raw frames. Answers the hub's PINGs itself."""

    def __init__(self, ws, session: str, limits: dict):
        self.ws, self.session, self.limits = ws, session, limits
        self.q: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, str):
                    continue
                f = lt.decode(raw)
                if f.type == lt.PING and not f.is_pong:
                    await self.send(lt.pong_for(f))
                else:
                    self.q.put_nowait(f)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            self.q.put_nowait(Closed(f"read error {e!r}"))
            return
        rc = self.ws.close_code if self.ws.close_code is not None else 1006
        self.q.put_nowait(Closed(rc))

    async def send(self, f: lt.Frame) -> None:
        await self.ws.send(lt.encode(f))

    async def next(self, timeout: float = 10.0, types=None) -> lt.Frame:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            try:
                f = await asyncio.wait_for(self.q.get(), max(left, 0.001))
            except TimeoutError:
                raise Failure(f"no frame within {timeout:g}s") from None
            if isinstance(f, Closed):
                self.q.put_nowait(f)
                raise f
            if types is None or f.type in types:
                return f

    async def closed_code(self, timeout: float = 40.0):
        """Wait for the hub to close this connection; return the close code."""
        try:
            while True:
                await self.next(timeout)
        except Closed as c:
            return c.code

    async def drain(self, quiet: float = 0.3) -> list:
        out = []
        while True:
            try:
                out.append(await self.next(quiet))
            except Failure:
                return out

    async def read_request(self, timeout: float = 10.0):
        """The next OPEN, then frames to END. Returns (stream id, open dict, body bytes); credits what it reads."""
        o = await self.next(timeout, (lt.OPEN,))
        body = b""
        while True:
            f = await self.next(timeout, (lt.DATA, lt.END, lt.RESET))
            expect(f.stream_id == o.stream_id, "frame for another stream")
            if f.type == lt.END:
                return o.stream_id, o.json(), body
            expect(f.type == lt.DATA, f"stream reset by the hub: {f.payload!r}")
            body += f.payload
            await self.send(lt.credit_frame(o.stream_id, len(f.payload)))

    async def answer(self, sid: int, status: int, headers: list, body: bytes = b"") -> None:
        """RESPONSE, the body in DATA frames within the hub's window, END."""
        await self.send(lt.response_frame(sid, status, headers))
        window, off = lt.INITIAL_WINDOW, 0
        while off < len(body):
            if window <= 0:
                f = await self.next(10.0, (lt.CREDIT, lt.RESET))
                expect(f.type == lt.CREDIT, "hub reset the response stream")
                window += lt.credit_amount(f)
                continue
            n = min(lt.MAX_DATA, len(body) - off, window)
            await self.send(lt.data_frame(sid, body[off:off + n]))
            window -= n
            off += n
        await self.send(lt.end_frame(sid))

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass
        self.task.cancel()


async def connect_tunnel(net: Net, url: str, hello_for, *, subprotocols=(lt.SUBPROTOCOL,)):
    """Open the WebSocket, read the challenge, answer with hello_for(challenge) (a str, or None to send
    nothing). Returns (ws, challenge)."""
    ws = await net.ws(url, subprotocols=list(subprotocols))
    try:
        challenge = lt.parse_text_message(await asyncio.wait_for(ws.recv(), 10))
    except Exception:
        await ws.close()
        raise
    expect(challenge["t"] == "challenge", "first message is not a challenge")
    h = hello_for(challenge)
    if h is not None:
        await ws.send(h)
    return ws, challenge


async def open_peer(net: Net, url: str, inst: Inst) -> Peer:
    ws, _ = await connect_tunnel(net, url, inst.hello)
    msg = lt.parse_text_message(await asyncio.wait_for(ws.recv(), 10))
    expect(msg["t"] == "ready", "expected ready")
    return Peer(ws, msg["session"], msg["limits"])


def new_name(prefix: str = "cf") -> str:
    return prefix + secrets.token_hex(4)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stale(seconds: int = 1000) -> str:
    return format_time(now() - seconds)
