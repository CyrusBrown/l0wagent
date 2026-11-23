"""Echo: the test extension for the hub's `echo` service. Read it next to docs/EXTENDING.md (ext.hub)."""

from __future__ import annotations

import json

from fastapi import HTTPException
from pydantic import BaseModel

from l0w_control.sdk import Extension, HubCallError, HubUnavailable


class Say(BaseModel):
    text: str = "hello"


def setup(ext: Extension) -> None:
    @ext.router.post("/say")
    async def say(body: Say):
        """Send the text to echo and return what comes back (the reply is untrusted content from the service)."""
        try:
            return await ext.hub.acall("echo", "echo", {"text": body.text}, timeout=30)
        except HubUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except HubCallError as e:
            raise HTTPException(502, f"echo said {e.code}: {e}") from e

    def on_echo_event(event: dict) -> None:
        n = int(ext.store.get("count", 0)) + 1
        ext.store.set("count", n)
        ext.folders.write("events", f"{n:04d}-{event['event'].replace('/', '_')}.json", json.dumps(event, indent=1))

    ext.hub.on_event("custom", on_echo_event)

    @ext.router.get("/events")
    def events():
        return {"count": ext.store.get("count", 0)}
