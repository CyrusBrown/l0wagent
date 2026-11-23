"""The test extension in dev/hub/echo-extension loads with control's loader, calls echo and keeps echo's events."""

import json
import shutil
import time
from pathlib import Path

from .hubworld import ECHO, wait_sent
from .test_extensions import in_thread, ok, wait_for

SRC = Path(__file__).resolve().parents[2] / "dev" / "hub" / "echo-extension"


def test_echo_extension_loads_calls_echo_and_writes_events(hw):
    shutil.copytree(SRC, Path(hw.be.ext.root) / "echo", ignore=shutil.ignore_patterns("README.md"))
    ok(hw.c.post("/api/extensions/rescan"))
    ok(hw.c.put("/api/extensions/echo/enabled", json={"enabled": True}))
    assert any(e["name"] == "echo" and e["loaded"] and e["compatible"] for e in ok(hw.c.get("/api/extensions")))
    # no hub service connected yet: a 503, not a crash
    assert hw.c.post("/api/ext/echo/say", json={"text": "x"}).status_code == 503
    hw.connect_echo()
    f = in_thread(hw.c.post, "/api/ext/echo/say", json={"text": "hi"})
    [out] = wait_sent(hw)
    assert out["body"]["method"] == "echo" and out["body"]["args"] == {"text": "hi"}
    hw.reply(out["body"]["call"], ok=True, result={"text": "hi"})
    assert f.result(10).json() == {"text": "hi"}
    # an event from echo ends up in the managed folder
    assert hw.arrive(ECHO, "service-event", {"event": "post.new", "data": {"title": "t"}}).status_code == 202
    folder = Path(hw.be.ext.root).parent / "work" / "_inbox" / "_ext" / "echo" / "events"
    assert wait_for(lambda: folder.is_dir() and any(folder.iterdir()))
    [file] = list(folder.iterdir())
    assert file.name == "0001-post.new.json" and json.loads(file.read_text())["data"] == {"title": "t"}
    time.sleep(0.1)
    assert ok(hw.c.get("/api/ext/echo/events")) == {"count": 1}
