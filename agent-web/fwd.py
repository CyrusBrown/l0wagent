"""agent-web (carried over from the old assistant): forwards the user's LAN port 8787 to the agent's port 8787.

The agent's only network is `sandbox`, which is internal, and Docker won't publish ports for a
container on internal networks only. This tiny TCP forwarder sits on `outside` (to publish the
port) and `sandbox` (to reach the agent), and only ever connects to TARGET: the agent can't use
it to reach anything. Inbound only: LAN clients can reach what the agent serves, nothing more.
"""
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s agent-web: %(message)s")
log = logging.getLogger("agent-web")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8787"))
TARGET_HOST = os.environ.get("TARGET_HOST", "agent")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "8787"))

DOWN = (
    "HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n"
    "Nothing is listening on the assistant's port 8787 right now.\n"
).encode()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(TARGET_HOST, TARGET_PORT), timeout=5)
    except Exception:  # noqa: BLE001
        try:
            client_w.write(DOWN)
            await client_w.drain()
        finally:
            client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    log.info("forwarding :%s -> %s:%s", LISTEN_PORT, TARGET_HOST, TARGET_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
