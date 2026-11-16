import importlib.util
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "cli" / "migrate_export.py"
SECRET = "sk-super-secret-value-123"


def load(monkeypatch, old: Path, work: Path, new_home: Path):
    monkeypatch.setenv("OLD_ROOT", str(old))
    monkeypatch.setenv("WORK_ROOT", str(work))
    monkeypatch.setenv("NEW_HOME", str(new_home))
    spec = importlib.util.spec_from_file_location("migrate_export", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def old(tmp_path):
    root = tmp_path / "old"
    (root / "control").mkdir(parents=True)
    (root / "egress").mkdir()
    db = root / "control" / "assistant.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    for ddl in [
        "projects(id INTEGER PRIMARY KEY, slug, name, summary, status, doc_path, links, created_by, created_at, updated_at)",
        "messages(id INTEGER PRIMARY KEY, created_at, sender, body, run_id, delivered_at, project_id, via)",
        "questions(id INTEGER PRIMARY KEY, created_at, run_id, project_id, title, details, kind, options, default_answer, status, answer, note, answered_at)",
        "permissions(id INTEGER PRIMARY KEY, created_at, run_id, project_id, title, details, status, response, decided_at, actions, action_results)",
        "tasks(id INTEGER PRIMARY KEY, created_at, run_id, project_id, title, details, status, note, resolved_at)",
        "runs(id INTEGER PRIMARY KEY, mode, trigger, model, effort, status, reason, created_at, started_at, ended_at, num_turns, cost_usd, result)",
        "run_notes(id INTEGER PRIMARY KEY, run_id, ts, text, project_id)",
        "run_events(id INTEGER PRIMARY KEY, run_id, ts, type, data)",
        "reviewer_messages(id INTEGER PRIMARY KEY, created_at, day, run_id, sender, kind, body)",
        "notifications(id INTEGER PRIMARY KEY, created_at, source, body, run_id, project_id)",
        "settings(key PRIMARY KEY, value, updated_at)",
        "secrets(name PRIMARY KEY, value, description, created_at, updated_at, last_read_at, read_count)",
    ]:
        con.execute(f"CREATE TABLE {ddl}")
    t = "2026-11-16T09:30:00+00:00"
    con.execute("INSERT INTO projects VALUES (1,'ml','ML research','s','active','p.md','[]','assistant',?,?)", (t, t))
    con.execute("INSERT INTO messages VALUES (1,?,'user','hello general',NULL,NULL,NULL,'web')", (t,))
    con.execute("INSERT INTO messages VALUES (2,'2026-11-16T09:31:00+00:00','assistant','training started',5,NULL,1,NULL)")
    con.execute("INSERT INTO questions VALUES (1,?,5,1,'Use GPU?','d','single','[\"a\",\"b\"]','a','answered','\"b\"','go',?)", (t, t))
    con.execute("INSERT INTO permissions VALUES (1,?,5,1,'Install pkg','d','approved','ok',?,'[]',NULL)", (t, t))
    con.execute("INSERT INTO tasks VALUES (1,?,5,NULL,'Pay bill','d','done','paid',?)", (t, t))
    con.execute("INSERT INTO runs VALUES (5,'work','wakeup','m','high','done',NULL,?,?,?,3,0.5,'all good')", (t, t, t))
    con.execute("INSERT INTO run_notes VALUES (1,5,?,'a note',1)", (t,))
    con.execute("INSERT INTO run_events VALUES (1,5,?,'text','{\"x\":1}')", (t,))
    con.execute("INSERT INTO reviewer_messages VALUES (1,?,'2026-11-16',5,'reviewer','review','looks fine')", (t,))
    con.execute("INSERT INTO notifications VALUES (1,?,'run','n',5,1)", (t,))
    con.execute("INSERT INTO settings VALUES ('level','busy',?)", (t,))
    con.execute(f"INSERT INTO secrets VALUES ('GH_TOKEN','{SECRET}','github',?,?,NULL,0)", (t, t))
    con.commit()
    assert (root / "control" / "assistant.db-wal").stat().st_size > 0
    old_con = con  # keep open so the WAL is not checkpointed
    egress = sqlite3.connect(root / "egress" / "egress.db")
    egress.execute("CREATE TABLE rules(id INTEGER PRIMARY KEY, pattern, action, note, created_at)")
    egress.execute("INSERT INTO rules VALUES (1,'192.168.1.5','allow_ro','jellyfin',?)", (t,))
    egress.commit()
    egress.close()

    ws = root / "workspace"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / "notes.md").write_text("hi")
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o700)
    (ws / "link").symlink_to("notes.md")

    home = root / "home"
    (home / "venv").mkdir(parents=True)
    (home / "venv" / "x").write_text("x")
    (home / ".cache").mkdir()
    (home / ".cache" / "c").write_text("c")
    (home / "ml-research-repo").mkdir()
    (home / "ml-research-repo" / "a.py").write_text("print(1)")
    cl = home / ".claude"
    (cl / "projects" / "x" / "memory").mkdir(parents=True)
    (cl / "CLAUDE.md").write_text("# memory")
    (cl / ".credentials.json").write_text('{"token":"cred"}')
    (cl / "projects" / "x" / "memory" / "m.md").write_text("m")
    (cl / "projects" / "x" / "t.jsonl").write_text("transcript")
    (cl / "todos").mkdir()
    (cl / "todos" / "t").write_text("t")
    yield root
    old_con.close()


def test_export(monkeypatch, tmp_path, old):
    work = tmp_path / "work"
    work.mkdir()
    mod = load(monkeypatch, old, work, tmp_path / "newhome")
    mod.do_export(False)
    leg = work / "_legacy"
    data = json.loads((leg / "control-export" / "export.json").read_text())
    assert [p["slug"] for p in data["projects"]] == ["ml"]
    assert len(data["messages"]) == 2 and data["messages"][0]["body"] == "hello general"
    assert data["questions"][0]["answer"] == "b" and data["questions"][0]["options"] == ["a", "b"]
    assert data["permissions"][0]["status"] == "approved"
    assert data["runs"][0]["result"] == "all good" and "events" not in data["runs"][0]
    assert data["run_notes"][0]["text"] == "a note"
    assert data["reviewer_messages"][0]["body"] == "looks fine"
    assert data["settings"][0]["key"] == "level"
    assert data["secret_names"] == [{"name": "GH_TOKEN", "description": "github"}]
    assert data["egress_rules"][0]["action"] == "allow_ro"
    assert "exported_at" in data and data["notifications"] and data["tasks"]

    for p in leg.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert SECRET.encode() not in p.read_bytes(), p

    assert (leg / "workspace" / ".git" / "HEAD").is_file()
    assert (leg / "workspace" / "link").is_symlink()
    assert os.readlink(leg / "workspace" / "link") == "notes.md"
    h = leg / "home"
    assert (h / "ml-research-repo" / "a.py").is_file()
    assert not (h / "venv").exists() and not (h / ".cache").exists()
    assert (h / ".claude" / "CLAUDE.md").is_file()
    assert (h / ".claude" / "projects" / "x" / "memory" / "m.md").is_file()
    assert not (h / ".claude" / "projects" / "x" / "t.jsonl").exists()
    assert not (h / ".claude" / "todos").exists()
    assert not list(leg.rglob(".credentials.json"))

    ce = leg / "control-export"
    general = (ce / "threads" / "general.md").read_text()
    assert "hello general" in general and "Pay bill" in general and "paid" in general
    ml = (ce / "threads" / "ml.md").read_text()
    assert "training started" in ml and "Use GPU?" in ml and "Install pkg" in ml
    assert "all good" in (ce / "runs.md").read_text() and "a note" in (ce / "runs.md").read_text()
    assert "looks fine" in (ce / "reviewer.md").read_text()
    assert json.loads((ce / "run-events" / "5.jsonl").read_text())["data"] == {"x": 1}
    assert (ce / "README.md").is_file()

    assert stat.S_IMODE((leg / "workspace" / "notes.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((leg / "workspace" / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((leg / "workspace" / ".git").stat().st_mode) == 0o755


def test_refuses_without_force(monkeypatch, tmp_path, old):
    work = tmp_path / "work"
    work.mkdir()
    mod = load(monkeypatch, old, work, tmp_path / "nh")
    mod.do_export(False)
    with pytest.raises(SystemExit):
        mod.do_export(False)
    (work / "_legacy" / "marker").write_text("x")
    mod.do_export(True)
    assert not (work / "_legacy" / "marker").exists()


def test_secrets_mode(monkeypatch, tmp_path, old, capsys):
    mod = load(monkeypatch, old, tmp_path / "w", tmp_path / "nh")
    mod.do_secrets()
    out = json.loads(capsys.readouterr().out)
    assert out == {"secrets": [{"name": "GH_TOKEN", "value": SECRET, "description": "github"}]}
    assert not (tmp_path / "w").exists()


def test_claude_login(monkeypatch, tmp_path, old):
    nh = tmp_path / "nh"
    mod = load(monkeypatch, old, tmp_path / "w", nh)
    mod.do_claude_login(False)
    dst = nh / ".claude" / ".credentials.json"
    assert dst.read_text() == '{"token":"cred"}'
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    dst.write_text("new")
    mod.do_claude_login(False)
    assert dst.read_text() == "new"
    mod.do_claude_login(True)
    assert dst.read_text() == '{"token":"cred"}'
