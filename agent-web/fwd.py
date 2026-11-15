"""agent-web: forwards the owner's LAN port 8787 to the agent's port 8787.

The agent's only network is `sandbox`, which is internal, so Docker won't publish its ports.
This forwarder sits on `outside` (to publish the port) and `sandbox` (to reach the agent),
and only ever connects to TARGET: the agent can't use it to reach anything.

It speaks just enough HTTP/1.1 to protect the owner. Browsers send a host's cookies to every
port on it, so without this the agent's page would receive the owner's control session.
- Requests lose `Cookie`, `Authorization` and `Proxy-Authorization`.
- Responses lose `Set-Cookie` (the agent can't plant or overwrite cookies for the host).
- After a protocol upgrade (WebSockets) bytes flow untouched; credentials only travel in the
  handshake, which was already filtered.
Anything it can't parse closes the connection.
"""

import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s agent-web: %(message)s")
log = logging.getLogger("agent-web")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8787"))
TARGET_HOST = os.environ.get("TARGET_HOST", "agent")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "8787"))
HEAD_MAX = 64 * 1024
STRIP_REQUEST = {b"cookie", b"authorization", b"proxy-authorization"}
STRIP_RESPONSE = {b"set-cookie", b"set-cookie2"}

DOWN = (
    "HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n"
    "Nothing is listening on the assistant's port 8787 right now.\n"
).encode()


class Bad(Exception):
    pass


async def read_head(r: asyncio.StreamReader) -> bytes | None:
    try:
        return await r.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as e:
        if e.partial.strip():
            raise Bad("truncated head") from e
        return None
    except asyncio.LimitOverrunError as e:
        raise Bad("head too large") from e


def filter_head(head: bytes, strip: set[bytes]) -> tuple[bytes, bytes, dict[bytes, bytes]]:
    """(start line, filtered head, lower-cased headers kept)."""
    lines = head[:-4].split(b"\r\n")
    kept, headers = [lines[0]], {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or line[:1] in (b" ", b"\t"):
            raise Bad("malformed or folded header")
        key = name.strip().lower()
        if key in strip:
            continue
        headers[key] = value.strip()
        kept.append(line)
    return lines[0], b"\r\n".join(kept) + b"\r\n\r\n", headers


async def relay_body(r: asyncio.StreamReader, w: asyncio.StreamWriter, headers: dict[bytes, bytes], until_eof: bool) -> None:
    if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
        while True:
            size_line = await r.readuntil(b"\r\n")
            w.write(size_line)
            size = int(size_line.split(b";")[0].strip(), 16)
            if size == 0:
                while True:  # trailers, then the blank line
                    line = await r.readuntil(b"\r\n")
                    w.write(line)
                    if line == b"\r\n":
                        break
                break
            w.write(await r.readexactly(size + 2))
            await w.drain()
    elif b"content-length" in headers:
        left = int(headers[b"content-length"])
        while left > 0:
            chunk = await r.read(min(left, 65536))
            if not chunk:
                raise Bad("body cut short")
            w.write(chunk)
            left -= len(chunk)
            await w.drain()
    elif until_eof:
        while chunk := await r.read(65536):
            w.write(chunk)
            await w.drain()
    await w.drain()


async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:  # noqa: BLE001
        pass


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(TARGET_HOST, TARGET_PORT, limit=HEAD_MAX), timeout=5)
    except Exception:  # noqa: BLE001
        try:
            cw.write(DOWN)
            await cw.drain()
        finally:
            cw.close()
        return
    methods: asyncio.Queue[bytes] = asyncio.Queue()
    upgraded = asyncio.Event()

    async def requests() -> None:
        while True:
            head = await read_head(cr)
            if head is None:
                return
            start, filtered, headers = filter_head(head, STRIP_REQUEST)
            methods.put_nowait(start.split(b" ", 1)[0].upper())
            uw.write(filtered)
            if b"upgrade" in headers.get(b"connection", b"").lower():
                await uw.drain()
                await upgraded.wait()
                await pipe(cr, uw)
                return
            await relay_body(cr, uw, headers, until_eof=False)

    async def responses() -> None:
        while True:
            head = await read_head(ur)
            if head is None:
                return
            start, filtered, headers = filter_head(head, STRIP_RESPONSE)
            parts = start.split(b" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            cw.write(filtered)
            if status == 101:
                await cw.drain()
                upgraded.set()
                await pipe(ur, cw)
                return
            if 100 <= status < 200:
                continue  # interim response; the real one follows for the same request
            method = await methods.get()
            if method != b"HEAD" and status not in (204, 304):
                close_delimited = b"content-length" not in headers and b"chunked" not in headers.get(b"transfer-encoding", b"").lower()
                await relay_body(ur, cw, headers, until_eof=close_delimited)
                if close_delimited:
                    return
            await cw.drain()

    tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception() and not isinstance(t.exception(), (ConnectionError, asyncio.IncompleteReadError)):
                log.info("closing a connection: %s", t.exception())
        # Requests finished cleanly: let the responses drain, then close.
        if tasks[0].done() and not tasks[0].exception():
            await asyncio.wait_for(tasks[1], timeout=300)
    except Exception:  # noqa: BLE001
        pass
    finally:
        for t in tasks:
            t.cancel()
        for w in (cw, uw):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


async def main() -> None:
    server = await asyncio.start_server(handle, os.environ.get("LISTEN_HOST", "0.0.0.0"), LISTEN_PORT, limit=HEAD_MAX)
    log.info("forwarding :%s -> %s:%s (stripping cookies and credentials)", LISTEN_PORT, TARGET_HOST, TARGET_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
