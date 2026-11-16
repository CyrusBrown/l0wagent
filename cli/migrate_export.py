#!/usr/bin/env python3
"""Export the old assistant for `l0w migrate`. Runs inside a python:3.12-slim container (stdlib only).

  export        old control DB, egress rules, workspace and home -> /work/_legacy/
  secrets       print {"secrets": [...]} to stdout (never to disk)
  claude-login  copy the old Claude credentials into the new agent home

Paths come from OLD_ROOT (default /old), WORK_ROOT (/work) and NEW_HOME (/new-home).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

OLD = Path(os.environ.get("OLD_ROOT", "/old"))
WORK = Path(os.environ.get("WORK_ROOT", "/work"))
NEW_HOME = Path(os.environ.get("NEW_HOME", "/new-home"))
AGENT_UID = 1000

HOME_DROP = {"venv", ".cache", ".npm", ".local", ".triton", ".nv", ".aspnet"}
CLAUDE_KEEP = {"CLAUDE.md", "settings.json", "agents", "skills", "commands", "projects"}

TABLES = ["projects", "messages", "questions", "permissions", "tasks", "runs", "run_notes",
          "reviewer_messages", "notifications", "settings"]
JSON_COLS = {"links", "options", "answer", "actions", "action_results", "data"}


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def open_copy(src: Path, tmp: Path) -> sqlite3.Connection:
    if not src.is_file():
        fail(f"{src} not found")
    dst = tmp / src.name
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.is_file():
            shutil.copy2(s, str(dst) + suffix)
    con = sqlite3.connect(str(dst))
    con.row_factory = sqlite3.Row
    return con


def rows(con: sqlite3.Connection, table: str, order: str = "id") -> list[dict]:
    try:
        cur = con.execute(f"SELECT * FROM {table} ORDER BY {order}")
    except sqlite3.OperationalError:
        return []
    out = []
    for r in cur:
        d = dict(r)
        for k in JSON_COLS & d.keys():
            if isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        out.append(d)
    return out


def old_db_paths() -> tuple[Path, Path]:
    return OLD / "control" / "assistant.db", OLD / "egress" / "egress.db"


# ── export ────────────────────────────────────────────────────────────────
def copy_home(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name in HOME_DROP:
            continue
        if entry.name == ".claude" and entry.is_dir() and not entry.is_symlink():
            copy_claude(entry, dst / ".claude")
            continue
        copy_entry(entry, dst / entry.name)


def copy_entry(src: Path, dst: Path) -> None:
    if src.is_symlink():
        dst.symlink_to(os.readlink(src))
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True, copy_function=shutil.copy2, ignore_dangling_symlinks=True)
    else:
        shutil.copy2(src, dst)


def copy_claude(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.name not in CLAUDE_KEEP:
            continue
        if entry.name == "projects":
            for proj in sorted(entry.iterdir()):
                mem = proj / "memory"
                if mem.is_dir():
                    copy_entry(mem, dst / "projects" / proj.name / "memory")
            continue
        copy_entry(entry, dst / entry.name)


def fence(text: str) -> str:
    return "\n".join("> " + ln if ln else ">" for ln in str(text).splitlines()) or ">"


def thread_markdown(title: str, msgs: list[dict], qs: list[dict], perms: list[dict], tasks: list[dict]) -> str:
    items: list[tuple[str, int, str]] = []
    for m in msgs:
        who = "You" if m.get("sender") == "user" else "Assistant"
        items.append((m.get("created_at") or "", 0, f"### {m.get('created_at')} {who}\n\n{m.get('body') or ''}\n"))
    for q in qs:
        s = f"### {q.get('created_at')} Question ({q.get('status')}): {q.get('title')}\n\n"
        if q.get("details"):
            s += f"{q['details']}\n\n"
        if q.get("options"):
            s += f"Options: {json.dumps(q['options'])}\n\n"
        if q.get("answer") is not None:
            s += f"Answer ({q.get('answered_at')}): {json.dumps(q['answer'])}\n\n"
        if q.get("note"):
            s += f"Note: {q['note']}\n\n"
        items.append((q.get("created_at") or "", 1, s))
    for p in perms:
        s = f"### {p.get('created_at')} Permission ({p.get('status')}): {p.get('title')}\n\n"
        if p.get("details"):
            s += f"{p['details']}\n\n"
        if p.get("actions"):
            s += f"Actions: {json.dumps(p['actions'])}\n\n"
        if p.get("response"):
            s += f"Response ({p.get('decided_at')}): {p['response']}\n\n"
        items.append((p.get("created_at") or "", 2, s))
    for t in tasks:
        s = f"### {t.get('created_at')} Task ({t.get('status')}): {t.get('title')}\n\n"
        if t.get("details"):
            s += f"{t['details']}\n\n"
        if t.get("note"):
            s += f"Note ({t.get('resolved_at')}): {t['note']}\n\n"
        items.append((t.get("created_at") or "", 3, s))
    items.sort(key=lambda x: (x[0], x[1]))
    return f"# {title}\n\n" + "\n".join(i[2] for i in items)


def write_threads(out: Path, data: dict) -> None:
    (out / "threads").mkdir(parents=True, exist_ok=True)
    projects = data["projects"]
    names = {None: ("general", "General")}
    for p in projects:
        names[p["id"]] = (p.get("slug") or f"project-{p['id']}", p.get("name") or p.get("slug") or f"Project {p['id']}")
    for pid, (slug, title) in names.items():
        sel = [[r for r in data[k] if r.get("project_id") == pid] for k in ("messages", "questions", "permissions", "tasks")]
        text = thread_markdown(title, *sel)
        (out / "threads" / f"{slug}.md").write_text(text)


def write_runs(out: Path, data: dict) -> None:
    notes: dict[int, list[dict]] = {}
    for n in data["run_notes"]:
        notes.setdefault(n["run_id"], []).append(n)
    parts = ["# Runs\n"]
    for r in data["runs"]:
        parts.append(f"## Run {r['id']}: {r.get('mode')} ({r.get('trigger')})\n")
        parts.append(f"- status: {r.get('status')}{' (' + r['reason'] + ')' if r.get('reason') else ''}")
        parts.append(f"- model: {r.get('model')} effort {r.get('effort')}")
        parts.append(f"- created {r.get('created_at')}, started {r.get('started_at')}, ended {r.get('ended_at')}")
        parts.append(f"- turns: {r.get('num_turns')}, cost: ${r.get('cost_usd')}\n")
        if r.get("result"):
            parts.append("Result:\n\n" + fence(r["result"]) + "\n")
        if notes.get(r["id"]):
            parts.append("Notes:\n")
            for n in notes[r["id"]]:
                parts.append(f"- {n.get('ts')}: {n.get('text')}")
            parts.append("")
    (out / "runs.md").write_text("\n".join(parts) + "\n")


def write_reviewer(out: Path, data: dict) -> None:
    parts = ["# Reviewer conversations\n"]
    day = object()
    for m in data["reviewer_messages"]:
        if m.get("day") != day:
            day = m.get("day")
            parts.append(f"\n## {day}\n")
        parts.append(f"**{m.get('sender')}** ({m.get('kind')}, {m.get('created_at')}, run {m.get('run_id')})\n\n{m.get('body')}\n")
    (out / "reviewer.md").write_text("\n".join(parts) + "\n")


README = """# Export of the old assistant

Written by `l0w migrate`. Everything here is a read-only record; secret values are not included.

- `export.json`: every control table as JSON (projects, messages, questions, permissions, tasks,
  runs without events, run_notes, reviewer_messages, notifications, settings), the names and
  descriptions of the secrets, and the egress rules.
- `threads/<project>.md`: each project's thread, and `general.md`, oldest first: messages, plus
  questions, permission requests and tasks with their answers.
- `runs.md`: one section per run with mode, status, times, cost, result and notes.
- `reviewer.md`: the reviewer conversations by day.
- `run-events/<run_id>.jsonl`: each run's raw events, for reference.

The old workspace is in `../workspace/` and the old home directory (without venvs and caches) in `../home/`.
"""


def do_export(force: bool) -> None:
    legacy = WORK / "_legacy"
    if legacy.exists() or legacy.is_symlink():
        if not force:
            fail(f"{legacy} already exists; use --force to replace it")
        if legacy.is_symlink() or legacy.is_file():
            legacy.unlink()
        else:
            shutil.rmtree(legacy)
    legacy.mkdir(parents=True)
    out = legacy / "control-export"
    out.mkdir()

    cdb, edb = old_db_paths()
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        con = open_copy(cdb, tmp)
        data: dict = {"exported_at": datetime.now(UTC).isoformat()}
        for t in TABLES:
            data[t] = rows(con, t, "key" if t == "settings" else "id")
        data["secret_names"] = [{"name": r["name"], "description": r["description"]}
                                for r in rows(con, "secrets", "name")]
        ev_dir = out / "run-events"
        ev_dir.mkdir()
        cur_run, fh = None, None
        try:
            for ev in con.execute("SELECT * FROM run_events ORDER BY run_id, id"):
                d = dict(ev)
                if isinstance(d.get("data"), str):
                    try:
                        d["data"] = json.loads(d["data"])
                    except ValueError:
                        pass
                if d["run_id"] != cur_run:
                    if fh:
                        fh.close()
                    cur_run = d["run_id"]
                    fh = open(ev_dir / f"{cur_run}.jsonl", "w")
                fh.write(json.dumps(d) + "\n")
        except sqlite3.OperationalError:
            pass
        finally:
            if fh:
                fh.close()
        con.close()
        egress = open_copy(edb, tmp)
        data["egress_rules"] = rows(egress, "rules")
        egress.close()

    (out / "export.json").write_text(json.dumps(data, indent=1))
    write_threads(out, data)
    write_runs(out, data)
    write_reviewer(out, data)
    (out / "README.md").write_text(README)

    ws = OLD / "workspace"
    if ws.is_dir():
        shutil.copytree(ws, legacy / "workspace", symlinks=True, copy_function=shutil.copy2, ignore_dangling_symlinks=True)
    home = OLD / "home"
    if home.is_dir():
        copy_home(home, legacy / "home")

    lock_down(legacy, out)
    print(f"Exported to {legacy}")


def lock_down(legacy: Path, out: Path) -> None:
    """Everything under _legacy, the control export included, is root's: the agent reads it, never changes it."""
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if is_root:
        os.chown(legacy, 0, 0)
    for dp, dns, fns in os.walk(legacy):
        for n in dns + fns:
            p = os.path.join(dp, n)
            if os.path.islink(p):
                if is_root:
                    os.lchown(p, 0, 0)
                continue
            if is_root:
                os.chown(p, 0, 0)
            if os.path.isdir(p):
                os.chmod(p, 0o755)
            else:
                os.chmod(p, 0o755 if os.stat(p).st_mode & 0o111 else 0o644)
    os.chmod(legacy, 0o755)
    os.chmod(out, 0o755)
    if is_root:
        os.chown(legacy, 0, 0)


def _under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


# ── secrets / claude-login ────────────────────────────────────────────────
def do_secrets() -> None:
    cdb, _ = old_db_paths()
    with tempfile.TemporaryDirectory() as tmpd:
        con = open_copy(cdb, Path(tmpd))
        items = [{"name": r["name"], "value": r["value"], "description": r["description"]}
                 for r in con.execute("SELECT name, value, description FROM secrets ORDER BY name")]
        con.close()
    json.dump({"secrets": items}, sys.stdout)
    sys.stdout.write("\n")


def do_claude_login(force: bool) -> None:
    src = OLD / "home" / ".claude" / ".credentials.json"
    dst = NEW_HOME / ".claude" / ".credentials.json"
    if not src.is_file():
        fail(f"no Claude credentials at {src}")
    if dst.exists() and not force:
        print("The new instance already has Claude credentials; leaving them (use --force to replace).")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(dst.parent, AGENT_UID, AGENT_UID)
        os.chown(dst, AGENT_UID, AGENT_UID)
    print("Copied the Claude login to the new assistant.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["export", "secrets", "claude-login"])
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    if a.mode == "export":
        do_export(a.force)
    elif a.mode == "secrets":
        do_secrets()
    else:
        do_claude_login(a.force)


if __name__ == "__main__":
    main()
