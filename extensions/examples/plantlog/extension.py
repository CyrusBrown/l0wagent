"""Plant log: the example extension. Read it next to docs/EXTENDING.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, Field

from l0w_control.sdk import Extension


class LogEntry(BaseModel):
    plant: str = Field(min_length=1, max_length=60)
    note: str = Field("", max_length=2000)
    watered: bool = False


def setup(ext: Extension) -> None:
    def now() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def plants() -> dict[str, dict]:
        return ext.store.get("plants", {})

    def due(p: dict) -> bool:
        last = p.get("last_watered")
        if not last:
            return True
        every = p.get("every_days") or ext.settings.get("water_every_days")
        return datetime.fromisoformat(last.replace("Z", "+00:00")) + timedelta(days=every) <= datetime.now(UTC)

    def view() -> list[dict]:
        return [{"name": n, **p, "due": due(p)} for n, p in sorted(plants().items())]

    def save(all_: dict[str, dict]) -> None:
        ext.store.set("plants", all_)
        # A readable copy for the agent, in the extension's managed folder.
        lines = ["# Plant log", "", "Written by the Plant log extension; edits here are overwritten.", ""]
        for name, p in sorted(all_.items()):
            lines.append(f"## {name}\n\nLast watered: {p.get('last_watered') or 'never'}\n")
            lines += [f"- {e['at']}: {e['note']}" for e in p.get("notes", [])[-20:]]
            lines.append("")
        ext.folders.write("log", "plants.md", "\n".join(lines))
        ext.emit("plants_changed")

    def record(entry: LogEntry, by: str) -> dict:
        all_ = plants()
        p = all_.setdefault(entry.plant.strip(), {"notes": []})
        if entry.note.strip():
            p["notes"].append({"at": now(), "note": entry.note.strip(), "by": by})
        if entry.watered:
            p["last_watered"] = now()
        save(all_)
        return {"name": entry.plant.strip(), **p, "due": due(p)}

    # ── owner routes: /api/ext/plantlog/… (the UI calls these) ───────────
    @ext.router.get("/plants")
    def list_plants() -> list[dict]:
        return view()

    @ext.router.post("/plants")
    def add_note(entry: LogEntry) -> dict:
        return record(entry, "owner")

    @ext.router.post("/plants/{name}/water")
    def water(name: str) -> dict:
        if name not in plants():
            raise HTTPException(404, f"No plant {name!r}")
        return record(LogEntry(plant=name, watered=True), "owner")

    # ── agent routes: /api/agent/ext/plantlog/… (`assistant ext plantlog …`) ──
    # Everything the agent sends is untrusted input: validate it like any outside request.
    @ext.agent_router.post("/plants")
    def agent_plants(body: dict | None = None) -> dict:
        rows = view()
        text = "\n".join(f"- {p['name']}: last watered {p.get('last_watered') or 'never'}{' (due)' if p['due'] else ''}" for p in rows)
        return {"text": text or "No plants yet.", "plants": rows}

    @ext.agent_router.post("/log")
    def agent_log(entry: LogEntry) -> dict:
        p = record(entry, "assistant")
        return {"text": f"Logged for {p['name']}."}

    # ── background: an Inbox card when a plant is due ─────────────────────
    def check_due() -> None:
        if not ext.settings.get("remind"):
            return
        carded = {c["data"].get("plant") for c in ext.inbox.open()}
        for p in view():
            if p["due"] and p["name"] not in carded:
                ext.inbox.card("water", f"Water {p['name']}", {"plant": p["name"], "last_watered": p.get("last_watered")})

    ext.every(3600, check_due)

    def on_card(event: dict) -> None:
        if event["kind"] == "water" and event["action"] == "watered":
            name = event["card"].get("plant")
            if name in plants():
                record(LogEntry(plant=name, watered=True), "owner")

    ext.on("extension_card_resolved", on_card)
