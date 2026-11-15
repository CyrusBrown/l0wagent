"""The agent's API (/api/agent/*), called from the sandbox by `assistant` and the hook.

The request guard admits only the agent token here, and refuses everything else from the
sandbox. Nothing here may approve permissions, answer the agent's own questions, finish
owner tasks, write secrets, change settings, start runs or change budgets. Everything the
agent sends is untrusted text: stored and shown, never executed or treated as an instruction.

Not part of the owner's OpenAPI contract (the CLI is its only client).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..runs.runtime import Item

if TYPE_CHECKING:
    from ..backends.sqlite import SqliteBackend

router = APIRouter(prefix="/api/agent", include_in_schema=False)
NOTE_MAX = 300
DESCRIBE_MAX = 1000


def _be(request: Request) -> SqliteBackend:
    return request.app.state.backend


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    run_id: int | None = None


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if not text:
        raise HTTPException(400, "empty text")
    return text[:limit]


def _run(be: SqliteBackend, claimed: int | None) -> int:
    """The active run. A run id from the agent is only a hint and must match it."""
    active = be.runs_mgr.active_run_id()
    if active is None:
        raise HTTPException(409, "no run in progress")
    if claimed is not None and claimed != active:
        raise HTTPException(409, f"run #{claimed} isn't the run in progress (#{active})")
    return active


@router.get("/ping")
def ping() -> dict:
    return {"ok": True}


@router.post("/notes")
def note(body: Text, request: Request) -> dict:
    be = _be(request)
    run_id = _run(be, body.run_id)
    text = _one_line(body.text, NOTE_MAX)
    be.db.ex("INSERT INTO run_notes(run_id, at, text) VALUES(?,?,?)", (run_id, be.db.now(), text))
    be.runs_mgr.timeline(run_id, Item("note", text, True))
    return {"run_id": run_id}


@router.post("/status")
def set_status(body: Text, request: Request) -> dict:
    be = _be(request)
    run_id = _run(be, body.run_id)
    text = _one_line(body.text, NOTE_MAX)
    be.db.ex("UPDATE runs SET now_sentence=? WHERE id=?", (text, run_id))
    be.runs_mgr.timeline(run_id, Item("status", text, False))
    be.runs_mgr._run_changed(run_id)
    return {"run_id": run_id}


@router.post("/run/describe")
def describe(body: Text, request: Request) -> dict:
    be = _be(request)
    run_id = _run(be, body.run_id)
    text = " ".join(body.text.split())[:DESCRIBE_MAX]
    be.db.ex("UPDATE runs SET description=? WHERE id=?", (text, run_id))
    be.runs_mgr._run_changed(run_id)
    return {"run_id": run_id}


@router.get("/status")
def get_status(request: Request) -> dict:
    be = _be(request)
    run_id = be.runs_mgr.active_run_id()
    run = be.runs_mgr.run(run_id) if run_id else None
    usage = be.usage.cached()
    lines = []
    if run:
        lines.append(f"Run #{run.id} ({run.kind}, {run.why}) · {run.tier} · deadline {run.deadline:%Y-%m-%d %H:%M} UTC" if run.deadline else f"Run #{run.id}")
        if run.now:
            lines.append(f"Now: {run.now}")
    else:
        lines.append("No run in progress.")
    if usage and usage.seven_day_pct is not None:
        five = f", 5-hour {usage.five_hour_pct:.0f}%" if usage.five_hour_pct is not None else ""
        lines.append(f"Claude usage: weekly {usage.seven_day_pct:.0f}%{five} (as of {usage.fetched_at:%H:%M} UTC)")
    else:
        lines.append("Claude usage: unknown")
    return {"text": "\n".join(lines), "run": run.model_dump(mode="json") if run else None, "usage": usage.model_dump(mode="json") if usage else None}


class Notify(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    run_id: int | None = None


@router.post("/notifications")
def notify(body: Notify, request: Request) -> dict:
    be = _be(request)
    nid = be.db.ex("INSERT INTO agent_notifications(run_id, body, created_at) VALUES(?,?,?)",
                   (be.runs_mgr.active_run_id(), be.vault.redact(body.body.strip()), be.db.now()))
    return {"id": nid}


@router.get("/inbox")
def inbox(request: Request, peek: bool = False, hook: bool = False) -> dict:
    """Everything new for the agent: owner messages, answers, decisions, finished tasks,
    suggestion outcomes, new secrets and its own notes. Marked delivered unless peeking."""
    be = _be(request)
    text, counts = be.conv.deliver(peek=peek)
    run_id = be.runs_mgr.active_run_id()
    if counts and not peek and run_id:
        from ..runs.manager import _counts_text

        how = "after a tool call" if hook else "on request"
        be.runs_mgr.timeline(run_id, Item("control", f"Delivered mid-run ({how}): {_counts_text(counts)}", True, {"counts": counts}))
    return {"text": text, "counts": counts}


@router.get("/news")
def news(request: Request) -> list[dict]:
    return _be(request).db.q("SELECT id, date, title, body FROM harness_news ORDER BY date DESC, id DESC")


# ── threads, projects, messages ─────────────────────────────────────────────
def _optional_run(be: SqliteBackend, claimed: int | None) -> int | None:
    active = be.runs_mgr.active_run_id()
    if claimed is not None and claimed != active:
        raise HTTPException(409, f"run #{claimed} isn't the run in progress")
    return active


@router.get("/projects")
def projects(request: Request, all: bool = False) -> list[dict]:
    be = _be(request)
    return [p.model_dump(mode="json") for p in be.conv.projects(None) if all or p.status != "archived"]


@router.get("/projects/{slug}")
def project(slug: str, request: Request) -> dict:
    be = _be(request)
    p = be.conv.project(slug).model_dump(mode="json")
    p["threads"] = [t.model_dump(mode="json") for t in be.conv.threads(slug, False, False)]
    return p


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = None
    summary: str = Field("", max_length=2000)
    doc_path: str | None = Field(None, max_length=300)
    links: list[dict] = []


@router.post("/projects")
def create_project(body: ProjectIn, request: Request) -> dict:
    links = [{"label": str(x.get("label", ""))[:80], "url": str(x.get("url", ""))[:500]} for x in body.links]
    return _be(request).conv.create_project(body.name, body.slug, body.summary, "assistant", body.doc_path, links).model_dump(mode="json")


class ProjectPatchIn(BaseModel):
    """The agent may keep a project's description current; renaming, archiving and
    marking stale go through suggestions."""

    summary: str | None = Field(None, max_length=2000)
    doc_path: str | None = Field(None, max_length=300)
    links: list[dict] | None = None


@router.patch("/projects/{slug}")
def patch_project(slug: str, body: ProjectPatchIn, request: Request) -> dict:
    fields = body.model_dump(exclude_none=True)
    return _be(request).conv.patch_project(slug, fields).model_dump(mode="json")


@router.get("/threads")
def threads(request: Request, project: str | None = None, general: bool = False, archived: bool = False) -> list[dict]:
    return [t.model_dump(mode="json") for t in _be(request).conv.threads(project, general, archived)]


class ThreadIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    project: str | None = None
    body: str | None = Field(None, max_length=100_000)
    run_id: int | None = None


@router.post("/threads")
def create_thread(body: ThreadIn, request: Request) -> dict:
    be = _be(request)
    t = be.conv.create_thread(body.title, body.project, "assistant", body.body, _optional_run(be, body.run_id))
    return t.model_dump(mode="json")


class MessageIn(BaseModel):
    thread: int
    body: str = Field(min_length=1, max_length=100_000)
    run_id: int | None = None


@router.post("/messages")
def send(body: MessageIn, request: Request) -> dict:
    be = _be(request)
    return be.conv.post_assistant_message(body.thread, body.body, _optional_run(be, body.run_id)).model_dump(mode="json")


@router.get("/messages")
def messages(request: Request, thread: int, limit: int = 30) -> dict:
    be = _be(request)
    t = be.conv.thread(thread)
    return {"thread": t.model_dump(mode="json"), "text": be.conv.thread_history(thread, max(1, min(limit, 200)))}


# ── asks ────────────────────────────────────────────────────────────────────
class QuestionIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: str = "yes_no"
    options: list[str] = []
    default: object = None
    scale_labels: list[str] | None = None
    thread: int | None = None
    run_id: int | None = None


@router.post("/questions")
def question(body: QuestionIn, request: Request) -> dict:
    be = _be(request)
    if body.kind not in ("yes_no", "single", "multi", "text", "slot", "scale"):
        raise HTTPException(400, "kind is yes_no, single, multi, text, slot or scale")
    q = be.conv.file_question(_optional_run(be, body.run_id), body.thread, body.kind, body.text, body.options, body.default, body.scale_labels)
    return q.model_dump(mode="json")


class PermissionIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str | None = Field(None, max_length=4000)
    actions: list[dict] = []
    thread: int | None = None
    run_id: int | None = None


@router.post("/permissions")
def permission(body: PermissionIn, request: Request) -> dict:
    be = _be(request)
    return be.conv.file_permission(_optional_run(be, body.run_id), body.thread, body.text, body.reason, body.actions).model_dump(mode="json")


class TaskIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    detail: str | None = Field(None, max_length=20_000)
    thread: int | None = None
    run_id: int | None = None


@router.post("/tasks")
def task(body: TaskIn, request: Request) -> dict:
    be = _be(request)
    return be.conv.file_task(_optional_run(be, body.run_id), body.thread, body.text, body.detail).model_dump(mode="json")


@router.get("/asks")
def asks(request: Request, status: str | None = None) -> list[dict]:
    be = _be(request)
    out = []
    for kind, table, model in (("question", "questions", be.conv.question_model), ("permission", "permissions", be.conv.permission_model),
                               ("task", "tasks", be.conv.task_model)):
        for r in be.db.q(f"SELECT * FROM {table} WHERE (? IS NULL OR status=?) ORDER BY id DESC LIMIT 100", (status, status)):
            out.append({"kind": kind, **model(r).model_dump(mode="json")})
    return sorted(out, key=lambda x: x["created_at"], reverse=True)


@router.post("/{kind}s/{id_}/withdraw")
def withdraw(kind: str, id_: int, request: Request) -> dict:
    if kind not in ("question", "permission", "task", "suggestion"):
        raise HTTPException(404, "not found")
    _be(request).conv.withdraw(kind, id_)
    return {"ok": True}


# ── claims and suggestions ──────────────────────────────────────────────────
class ClaimIn(BaseModel):
    thread: int
    plan: str = Field(min_length=1, max_length=2000)
    run_id: int | None = None


@router.post("/claims")
def claim(body: ClaimIn, request: Request) -> dict:
    be = _be(request)
    return be.conv.claim_thread(_run(be, body.run_id), body.thread, body.plan).model_dump(mode="json")


class ClaimDoneIn(BaseModel):
    thread: int
    result: str = Field(min_length=1, max_length=4000)
    status: str = "done"
    run_id: int | None = None


@router.post("/claims/done")
def claim_done(body: ClaimDoneIn, request: Request) -> dict:
    be = _be(request)
    if body.status not in ("done", "partial", "dropped"):
        raise HTTPException(400, "status is done, partial or dropped")
    return be.conv.close_claim(_run(be, body.run_id), body.thread, body.status, body.result).model_dump(mode="json")


class SuggestionIn(BaseModel):
    kind: str
    target: str = Field(description="Thread id, or project slug for project_* kinds.")
    text: str = Field(min_length=1, max_length=2000)
    payload: dict = {}
    run_id: int | None = None


@router.post("/suggestions")
def suggest(body: SuggestionIn, request: Request) -> dict:
    be = _be(request)
    return be.conv.suggest(_optional_run(be, body.run_id), body.kind, body.target, body.text, body.payload).model_dump(mode="json")


# ── secrets (the agent's to use) and search ─────────────────────────────────
@router.get("/secrets")
def secrets(request: Request) -> list[dict]:
    return [{"name": r["name"], "description": r["description"]} for r in _be(request).vault.listing()]


@router.get("/secrets/{name}")
def secret(name: str, request: Request) -> dict:
    be = _be(request)
    return {"name": name.upper(), "value": be.vault.get_for_agent(name, be.runs_mgr.active_run_id())}


@router.get("/search")
def search(request: Request, q: str, kind: str | None = None) -> list[dict]:
    return [h.model_dump(mode="json") for h in _be(request).conv.search(q, kind, None, None)]

