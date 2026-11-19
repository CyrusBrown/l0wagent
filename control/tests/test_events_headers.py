"""The live stream must reach the browser as it's written: proxies in front of a hub member (Cloudflare) compress and
buffer unless told not to, which stalled chat replies after their first words."""

import asyncio

from l0w_control.api import routes


def test_the_live_stream_forbids_transforming_and_buffering():
    class Be:
        async def events(self):
            if False:
                yield None

    r = asyncio.run(routes.events(Be()))
    assert "no-transform" in r.headers["cache-control"] and "no-store" in r.headers["cache-control"]
    assert r.headers["x-accel-buffering"] == "no" and r.media_type == "application/x-ndjson"
