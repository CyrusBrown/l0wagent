"""M7: importing the old assistant (see docs/plans/migration.md). Run by `./l0w migrate` through hostctl.

- `import_secrets(data)`: the old secrets (names, values, descriptions) piped in on stdin, straight
  from the old database to this vault; they're never written to disk on the way.
- `import_legacy(work)`: reads `/work/_legacy/control-export/export.json` (written by the exporter) and
  adds one **archived** thread per old project, plus one for the old General chat, with the old
  messages (their times kept) and the questions, permissions and tasks with their answers as system
  notes. It also opens the "Set yourself back up" thread with the re-setup brief
  (prompts/resetup.md), which the next run picks up.
- `import_egress(rules)`: the old egress rules, through egress's admin API.

Each step runs once: a kv marker records it, so re-running `./l0w migrate` doesn't duplicate anything.
Control keeps a cache of secrets, so the CLI restarts it after importing them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .db import Database
from .vault import Vault, validate_name

PROMPT = Path(__file__).parent / "prompts" / "resetup.md"
RESETUP_TITLE = "Set yourself back up after the move"
SENDERS = {"user": "owner", "assistant": "assistant"}


def _strip(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("<!--"):
            skip = "-->" not in line
            continue
        if skip:
            skip = "-->" not in line
            continue
        out.append(line)
    return "\n".join(out).strip()


def _marker(db: Database, key: str) -> str | None:
    row = db.q1("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else None


def _mark(db: Database, key: str, value: Any) -> None:
    db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
          (key, json.dumps(value), db.now()))


def import_secrets(db: Database, vault: Vault, data: dict) -> dict:
    done, skipped = [], []
    for s in data.get("secrets") or []:
        try:
            name = validate_name(str(s.get("name", "")))
        except Exception:  # noqa: BLE001 - an old name the new rules refuse: report it, don't stop
            skipped.append(str(s.get("name", ""))[:64])
            continue
        if vault.exists(name) or not str(s.get("value") or "").strip():
            skipped.append(name)
            continue
        vault.set(name, str(s["value"]), str(s.get("description") or "")[:200] or "From the old assistant")
        done.append(name)
    _mark(db, "legacy_secrets", {"imported": done, "skipped": skipped})
    return {"imported": done, "skipped": skipped}


def _note(kind: str, r: dict) -> str:
    """A question, permission or task from the old instance, as a system note with its outcome."""
    detail = (r.get("details") or "").strip()
    head = f"**Old {kind}:** {r.get('title', '')}" + (f"\n\n{detail}" if detail else "")
    if kind == "question":
        opts = r.get("options")
        if opts:
            try:
                head += "\n\nOptions: " + ", ".join(map(str, json.loads(opts)))
            except (ValueError, TypeError):
                pass
        answer = r.get("answer")
        try:
            answer = json.loads(answer) if answer else None
        except (ValueError, TypeError):
            pass
        if isinstance(answer, list):
            answer = ", ".join(map(str, answer))
        outcome = f"Answered: {answer}" if r.get("status") == "answered" else f"{r.get('status')}"
    elif kind == "permission":
        outcome = f"{r.get('status')}" + (f": {r['response']}" if r.get("response") else "")
    else:
        outcome = f"{r.get('status')}"
    if r.get("note"):
        outcome += f" (note: {r['note']})"
    return f"{head}\n\n→ {outcome}"


def import_legacy(db: Database, vault: Vault, export: dict) -> dict:
    if _marker(db, "legacy_threads"):
        return {"skipped": "already imported", **json.loads(_marker(db, "legacy_threads") or "{}")}
    projects = {p["id"]: p for p in export.get("projects") or []}
    groups: dict[Any, list[tuple[str, str, str, int | None]]] = {}  # project id → (at, sender, body, old run)
    for m in export.get("messages") or []:
        groups.setdefault(m.get("project_id"), []).append((m["created_at"], SENDERS.get(m.get("sender"), "system"), m.get("body") or "", m.get("run_id")))
    for kind, rows in (("question", export.get("questions")), ("permission", export.get("permissions")), ("task", export.get("tasks"))):
        for r in rows or []:
            groups.setdefault(r.get("project_id"), []).append((r["created_at"], "system", _note(kind, r), r.get("run_id")))
    now = db.now()
    made = []
    with db.tx() as tx:
        for pid, items in sorted(groups.items(), key=lambda kv: (kv[0] is not None, kv[0] or 0)):
            items.sort(key=lambda x: x[0])
            name = projects[pid]["name"] if pid in projects else ("General" if pid is None else f"Project #{pid}")
            first, last = items[0][0], items[-1][0]
            tid = tx.ex("INSERT INTO threads(project_id, title, status, created_by, created_at, updated_at, archived_at) VALUES(NULL,?,?,?,?,?,?)",
                        (f"{name} (old assistant)", "archived", "owner", first, last, now))
            mid = None
            for at, sender, body, old_run in items:
                text = vault.redact(body) or ""
                if old_run and sender == "assistant":
                    text += f"\n\n*(old run #{old_run})*"
                mid = tx.ex("INSERT INTO messages(thread_id, sender, body, via, delivery, created_at) VALUES(?,?,?,?,?,?)",
                            (tid, sender, text, "legacy", "seen", at))
            tx.ex("UPDATE threads SET last_read_message_id=? WHERE id=?", (mid, tid))
            made.append({"thread_id": tid, "title": f"{name} (old assistant)", "items": len(items)})
        resetup = tx.ex("INSERT INTO threads(project_id, title, status, created_by, pinned, created_at, updated_at) VALUES(NULL,?,?,?,?,?,?)",
                        (RESETUP_TITLE, "open", "owner", 1, now, now))
        tx.ex("INSERT INTO messages(thread_id, sender, body, via, created_at) VALUES(?,?,?,?,?)",
              (resetup, "owner", _strip(PROMPT.read_text()), "migrate", now))
    summary = {"threads": made, "resetup_thread_id": resetup, "counts": {k: len(export.get(k) or []) for k in
               ("projects", "messages", "questions", "permissions", "tasks", "runs", "run_notes", "reviewer_messages")}}
    _mark(db, "legacy_threads", summary)
    return summary


def import_egress(db: Database, rules: list[dict], add_rule) -> dict:
    """`add_rule(pattern, action, note)` is EgressClient.add_rule (async)."""
    if _marker(db, "legacy_egress"):
        return {"skipped": "already imported"}
    done, failed = [], []

    async def run() -> None:
        for r in rules:
            action = r.get("action")
            if action not in ("allow", "allow_ro", "block"):
                failed.append({"pattern": r.get("pattern"), "error": f"unknown action {action!r}"})
                continue
            try:
                await add_rule(str(r["pattern"]), action, (r.get("note") or "") + " (from the old assistant)")
                done.append(r["pattern"])
            except Exception as e:  # noqa: BLE001 - report each refusal (e.g. egress's own protected hosts)
                failed.append({"pattern": r.get("pattern"), "error": str(e)[:200]})

    asyncio.run(run())
    _mark(db, "legacy_egress", {"imported": done, "failed": failed})
    return {"imported": done, "failed": failed}
