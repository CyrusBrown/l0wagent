"""The tools the 1:1 chat and outside chat services (the MCP connector) use, written once.

They know the assistant through control's own backend: read projects, threads, runs, the
shelf, budgets and the wakeup plan, search, and read the agent's workspace (read-only, on
demand); write threads and projects as the owner ("via chat"); wake the assistant with Reply
now (the reply budget). Anything sensitive (a secret, a setting or budget change, an egress
rule, an extra wakeup) is only *proposed*, as a card the owner acts on in their own UI: no
tool here can set a secret, change a setting, approve anything or spend the allowance.

Each tool returns text for the model (compact JSON or a sentence). `kind` labels it:
read | write | wake | card. Outside clients get only the scopes they were granted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .api import schemas as s
from .backends.base import BadRequest, Conflict, NotFound
from .urls import safe_url
from .vault import store_text

MAX_RESULT = 12_000
FILE_MAX = 60_000


@dataclass
class Ctx:
    """Who is calling: a chat (chat_id) or an outside client (client label)."""

    be: Any                       # the backend
    chat_id: int | None = None
    client: str | None = None     # e.g. "connector:Claude Desktop"
    base_url: str | None = None   # outside clients get https links; the in-app chat gets l0wagent:// ones
    cards: list[dict] = field(default_factory=list)  # cards proposed during this turn

    def link(self, thread_id: int) -> str:
        return f"{self.base_url}/t/{thread_id}" if self.base_url else f"l0wagent://thread/{thread_id}"

    @property
    def via(self) -> str:
        return f"chat:{self.chat_id}" if self.chat_id else (self.client or "chat")


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str                     # read | write | wake | card
    description: str
    params: dict[str, Any]        # JSON schema properties
    required: tuple[str, ...]
    fn: Callable[[Ctx, dict], str]
    label: str                    # past tense, for the chat's "Read 3 threads · …" line

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required), "additionalProperties": False}


def _default(o: Any) -> Any:
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_none=True)
    return str(o)


def _j(obj: Any) -> str:
    if isinstance(obj, list):
        data = [o.model_dump(mode="json", exclude_none=True) if hasattr(o, "model_dump") else o for o in obj]
    elif hasattr(obj, "model_dump"):
        data = obj.model_dump(mode="json", exclude_none=True)
    else:
        data = obj
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_default)
    return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + f"… [{len(text) - MAX_RESULT} more characters cut]"


def _int(a: dict, k: str, default: int | None = None) -> int:
    v = a.get(k, default)
    if v is None:
        raise BadRequest("bad_input", f"{k} is required")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise BadRequest("bad_input", f"{k} must be a number") from None


def _str(a: dict, k: str, required: bool = True, max_len: int = 20_000) -> str | None:
    v = a.get(k)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise BadRequest("bad_input", f"{k} is required")
        return None
    if not isinstance(v, str):
        raise BadRequest("bad_input", f"{k} must be text")
    return v[:max_len]


# ── read ──────────────────────────────────────────────────────────────────
def status(ctx: Ctx, a: dict) -> str:
    st = ctx.be.status()
    return _j(st)


def list_projects(ctx: Ctx, a: dict) -> str:
    return _j([{"slug": p.slug, "name": p.name, "status": p.status, "summary": p.summary} for p in ctx.be.projects(None)])


def list_threads(ctx: Ctx, a: dict) -> str:
    project = _str(a, "project", required=False)
    threads = ctx.be.threads(project, False, bool(a.get("archived")))
    return _j([{"id": t.id, "title": t.title, "project": t.project, "status": t.status, "last_activity_at": t.last_activity_at, "open_asks": t.open_asks}
               for t in threads[:200]])


def read_thread(ctx: Ctx, a: dict) -> str:
    tid = _int(a, "thread_id")
    t = ctx.be.thread(tid)
    items = ctx.be.thread_items(tid, None, min(_int(a, "limit", 40), 200)).items
    out = []
    for i in items:
        d = i.model_dump(mode="json", exclude_none=True)
        msg = d.get("message")
        if msg:
            out.append({"type": "message", "id": msg["id"], "sender": msg["sender"], "at": msg["created_at"], "body": msg["body"]})
        else:
            out.append({k: v for k, v in d.items() if k in ("type", "question", "permission", "task", "claim", "suggestion", "promote", "card")})
    return _j({"thread": {"id": t.id, "title": t.title, "project": t.project, "status": t.status}, "items": out, "link": ctx.link(tid)})


def search(ctx: Ctx, a: dict) -> str:
    q = _str(a, "query", max_len=500)
    return _j(ctx.be.search(q, _str(a, "kind", required=False), _str(a, "project", required=False), None)[:30])


def list_runs(ctx: Ctx, a: dict) -> str:
    return _j(ctx.be.runs(None, _str(a, "project", required=False), min(_int(a, "limit", 10), 50)))


def read_run(ctx: Ctx, a: dict) -> str:
    rid = _int(a, "run_id")
    run = ctx.be.run(rid)
    tl = ctx.be.timeline(rid, "simple", None, None)
    return _j({"run": run, "timeline": [{"kind": i.kind, "text": i.text} for i in tl[-80:]]})


def shelf(ctx: Ctx, a: dict) -> str:
    return _j(ctx.be.shelf(_str(a, "project", max_len=100)))


def wakeup_plan(ctx: Ctx, a: dict) -> str:
    cal = ctx.be.calendar(None, None)
    return _j({"wakeups": cal.wakeups, "budget": ctx.be.budget_overview().providers[:1]})


def _volume(a: dict) -> str:
    v = _str(a, "volume", required=False) or "workspace"
    if v not in ("workspace", "work"):
        raise BadRequest("bad_input", "volume is workspace or work")
    return v


def list_files(ctx: Ctx, a: dict) -> str:
    return _j(ctx.be.list_dir(_volume(a), _str(a, "path", required=False) or ""))


def read_file(ctx: Ctx, a: dict) -> str:
    data, mime = ctx.be.read_file(_volume(a), _str(a, "path", max_len=1000))
    if b"\x00" in data[:4096]:
        return f"(a binary file, {mime}, {len(data)} bytes; not shown)"
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= FILE_MAX else text[:FILE_MAX] + f"\n… [{len(text) - FILE_MAX} more characters cut]"


# ── write (as the owner, labeled "via chat") ─────────────────────────────
def create_thread(ctx: Ctx, a: dict) -> str:
    title = _str(a, "title", max_len=200)
    body = _str(a, "message", max_len=50_000)
    project = _str(a, "project", required=False)
    conv = ctx.be.conv
    t = conv.create_thread(title, project, "chat")
    conv.post_owner_message(t.id, body, [], None, via=ctx.via)
    return _j({"created": {"thread_id": t.id, "title": t.title, "project": t.project}, "link": ctx.link(t.id)})


def post_to_thread(ctx: Ctx, a: dict) -> str:
    tid = _int(a, "thread_id")
    m = ctx.be.conv.post_owner_message(tid, _str(a, "message", max_len=50_000), [], None, via=ctx.via)
    return _j({"posted": {"thread_id": tid, "message_id": m.id}, "link": ctx.link(tid)})


def create_project(ctx: Ctx, a: dict) -> str:
    p = ctx.be.create_project(s.ProjectCreate(name=_str(a, "name", max_len=120) or "", summary=_str(a, "summary", required=False, max_len=2000) or "",
                                              idempotency_key=None))
    return _j({"created": {"slug": p.slug, "name": p.name}})


# ── wake ──────────────────────────────────────────────────────────────────
def reply_now(ctx: Ctx, a: dict) -> str:
    tid = _int(a, "thread_id")
    d = ctx.be.delivery(tid)
    if not d.reply_now_available:
        return f"Reply now isn't available for this thread right now (state: {d.state}). The assistant will see it at its next wakeup."
    run = ctx.be.reply_now(tid)
    return _j({"started": {"run_id": run.id, "budget_pts": run.budget, "bucket": run.bucket}, "note": "Spends the reply budget, not the allowance."})


# ── cards: proposals the owner acts on ───────────────────────────────────
def _card(ctx: Ctx, kind: str, **fields) -> str:
    card = {"kind": kind, **fields, "state": "open"}
    ctx.cards.append(card)
    where = "in this chat" if ctx.chat_id else "in the owner's l0wagent app (Inbox)"
    return f"Proposed: a {kind} card is shown {where}. Only the owner can act on it; you'll see the outcome in the conversation."


def _safe_link(a: dict, key: str) -> str | None:
    raw = _str(a, key, required=False, max_len=500)
    if not raw:
        return None
    url = safe_url(raw)
    if url is None:
        raise BadRequest("bad_input", f"{key} must be an http:// or https:// address")
    return url


def ask_for_secret(ctx: Ctx, a: dict) -> str:
    name = (_str(a, "name", max_len=64) or "").strip().upper()
    if not name.replace("_", "").isalnum():
        raise BadRequest("bad_input", "Secret names are letters, digits and underscores, e.g. GITHUB_TOKEN")
    return _card(ctx, "secret", name=name, description=_str(a, "description", required=False, max_len=500) or "", link=_safe_link(a, "help_url"))


def propose_integration(ctx: Ctx, a: dict) -> str:
    from .integrations import NAME_RE, PRESETS

    integ = getattr(ctx.be, "integrations", None)
    PRESETS = integ.all_presets() if integ else PRESETS  # noqa: N806  (the built-in ones plus enabled extensions')
    preset = (_str(a, "preset", required=False, max_len=32) or "").strip().lower()
    reason = _str(a, "reason", required=False, max_len=500) or ""
    if preset:
        p = PRESETS.get(preset)
        if not p:
            raise BadRequest("bad_input", f"Unknown preset {preset}. Presets: {', '.join(PRESETS)}. For anything else give name, title, url and secret_name.")
        return _card(ctx, "integration", preset=preset, name=preset, title=p["title"], url=p["url"], secret_name=p["auth_secret"],
                     guidance=p["guidance"], token_url=p["token_url"], reason=reason)
    name = (_str(a, "name", max_len=32) or "").strip().lower()
    if not NAME_RE.match(name):
        raise BadRequest("bad_input", "name looks like home-assistant: lowercase letters, digits and -, 2-32 characters")
    url = (_str(a, "url", max_len=500) or "").strip()
    if not url.startswith(("http://", "https://")):
        raise BadRequest("bad_input", "url is the MCP server's address and starts with http:// or https://")
    secret = (_str(a, "secret_name", max_len=64) or "").strip().upper()
    if not secret.replace("_", "").isalnum():
        raise BadRequest("bad_input", "secret_name is letters, digits and underscores, e.g. HA_TOKEN")
    return _card(ctx, "integration", preset=None, name=name, title=(_str(a, "title", required=False, max_len=80) or name).strip(), url=url,
                 secret_name=secret, guidance=_str(a, "guidance", required=False, max_len=1000) or "", token_url=_safe_link(a, "token_url"),
                 reason=reason)


def propose_wakeup(ctx: Ctx, a: dict) -> str:
    budget = _int(a, "budget")
    if not 1 <= budget <= 400:
        raise BadRequest("bad_input", "budget is 1–400 pts")
    return _card(ctx, "wakeup", at=_str(a, "at", required=False, max_len=40), budget=budget, reason=_str(a, "reason", max_len=500))


def propose_level(ctx: Ctx, a: dict) -> str:
    level = _str(a, "level", max_len=20)
    if level not in ("dormant", "frugal", "steady", "busy", "max"):
        raise BadRequest("bad_input", "level is dormant, frugal, steady, busy or max")
    return _card(ctx, "level", level=level, reason=_str(a, "reason", required=False, max_len=500))


def propose_egress_rule(ctx: Ctx, a: dict) -> str:
    host = (_str(a, "host", max_len=255) or "").strip().lower()
    if not host or "/" in host or " " in host:
        raise BadRequest("bad_input", "host is a domain, e.g. api.github.com")
    action = _str(a, "action", required=False) or "allow"
    if action not in ("allow", "block"):
        raise BadRequest("bad_input", "action is allow or block")
    return _card(ctx, "egress", host=host, action=action, reason=_str(a, "reason", required=False, max_len=500))


# ── help and settings ─────────────────────────────────────────────────────
GUIDE = __import__("pathlib").Path(__file__).parent / "guide"


def help_(ctx: Ctx, a: dict) -> str:
    """The built-in user guide: list topics, read one, or search."""
    topics = {p.stem: p.read_text() for p in sorted(GUIDE.glob("*.md"))}
    topic, query = _str(a, "topic", required=False, max_len=60), _str(a, "query", required=False, max_len=200)
    if topic:
        if topic not in topics:
            return f"No topic {topic}. Topics: {', '.join(topics)}."
        return topics[topic]
    if not query:
        return "Topics (read one with topic=…, or search with query=…):\n" + "\n".join(f"- {k}: {v.splitlines()[0].lstrip('# ')}" for k, v in topics.items())
    words = [w for w in query.lower().split() if len(w) > 2]
    hits = []
    for k, text in topics.items():
        for para in text.split("\n\n"):
            score = sum(para.lower().count(w) for w in words)
            if score:
                hits.append((score, k, para.strip()))
    hits.sort(key=lambda h: -h[0])
    return "\n\n".join(f"[{k}] {para}" for _, k, para in hits[:6]) or f"Nothing in the guide matches “{query}”. Topics: {', '.join(topics)}."


def read_settings(ctx: Ctx, a: dict) -> str:
    from .chat_settings import snapshot

    return _j(snapshot(ctx.be))


def propose_settings(ctx: Ctx, a: dict) -> str:
    from .chat_settings import describe

    changes = describe(ctx.be, a.get("changes"))
    return _card(ctx, "settings", changes=changes, reason=_str(a, "reason", required=False, max_len=500))


def propose_handoff(ctx: Ctx, a: dict) -> str:
    target = _str(a, "target", max_len=20)
    mode = _str(a, "mode", max_len=20)
    if target not in ("new_thread", "existing_thread", "new_project"):
        raise BadRequest("bad_input", "target is new_thread, existing_thread or new_project")
    if mode not in ("transcript", "message"):
        raise BadRequest("bad_input", "mode is transcript (the whole chat goes along) or message (only your hand-off message)")
    message = _str(a, "message", max_len=20_000)
    card: dict[str, Any] = {"target": target, "mode": mode, "message": message}
    if target == "existing_thread":
        t = ctx.be.thread(_int(a, "thread_id"))
        card.update(thread_id=t.id, thread_title=t.title)
    else:
        card["title"] = _str(a, "title", max_len=200)
        if target == "new_project":
            card["project_name"] = _str(a, "project_name", max_len=120)
        elif a.get("project"):
            card["project"] = ctx.be.project(_str(a, "project", max_len=100)).slug
    return _card(ctx, "handoff", **card)


P = {"type": "string"}
N = {"type": "integer"}
TOOLS: list[Tool] = [
    Tool("status", "read", "What the assistant is doing now, its budget this period, what's waiting for the owner.", {}, (), status, "checked the status"),
    Tool("list_projects", "read", "The owner's projects.", {}, (), list_projects, "listed projects"),
    Tool("list_threads", "read", "Threads, optionally in one project (slug), optionally archived ones.",
         {"project": P, "archived": {"type": "boolean"}}, (), list_threads, "listed threads"),
    Tool("read_thread", "read", "A thread's recent messages and cards (questions, permissions, tasks, claims).",
         {"thread_id": N, "limit": N}, ("thread_id",), read_thread, "read a thread"),
    Tool("search", "read", "Search threads, messages, projects and files.", {"query": P, "kind": P, "project": P}, ("query",), search, "searched"),
    Tool("list_runs", "read", "Recent runs (wakeups, replies, routines) with status and spend.", {"project": P, "limit": N}, (), list_runs, "listed runs"),
    Tool("read_run", "read", "One run and its timeline (simple view).", {"run_id": N}, ("run_id",), read_run, "read a run"),
    Tool("shelf", "read", "A project's shelf: the deliverables the assistant put on its Files tab.", {"project": P}, ("project",), shelf, "checked the shelf"),
    Tool("wakeup_plan", "read", "The assistant's planned wakeups and this period's budget.", {}, (), wakeup_plan, "checked the wakeup plan"),
    Tool("list_files", "read", "List a folder in the assistant's storage (volume workspace = its memory, work = project work). Read-only.",
         {"volume": P, "path": P}, (), list_files, "browsed the workspace"),
    Tool("read_file", "read", "Read a text file from the assistant's storage (read-only).", {"volume": P, "path": P}, ("path",), read_file, "read a file"),
    Tool("create_thread", "write", "Start a thread for the assistant (as the owner, labeled via chat), optionally in a project. "
         "Use it to hand work over: the assistant picks it up at its next run or a Reply now.",
         {"title": P, "message": P, "project": P}, ("title", "message"), create_thread, "started a thread"),
    Tool("post_to_thread", "write", "Post a message into an existing thread as the owner (labeled via chat).",
         {"thread_id": N, "message": P}, ("thread_id", "message"), post_to_thread, "posted to a thread"),
    Tool("create_project", "write", "Create a project.", {"name": P, "summary": P}, ("name",), create_project, "created a project"),
    Tool("reply_now", "wake", "Wake the assistant now to answer one thread (Reply now). Spends its small reply budget, never the allowance.",
         {"thread_id": N}, ("thread_id",), reply_now, "woke the assistant"),
    Tool("ask_for_secret", "card", "Ask the owner to enter a secret (an API token, a password) on a secure input card. "
         "The value goes from their browser straight to control; you never see it, only that it was saved. Link the provider's token page.",
         {"name": P, "description": P, "help_url": P}, ("name",), ask_for_secret, "asked for a secret"),
    Tool("propose_integration", "card", "Show a Connect integration card so the owner can connect a service (GitHub or another MCP server). "
         "First explain the steps in your own words: open the provider's token page (give the link) and create a token with the exact permissions needed, "
         "then paste it on the card. Never ask for the token in the conversation; the card has a secure input and the value goes straight to control. "
         "Use preset for a known service (github); otherwise give name, title, url and secret_name.",
         {"preset": P, "name": P, "title": P, "url": P, "secret_name": P, "guidance": P, "token_url": P, "reason": P}, (), propose_integration, "proposed an integration"),
    Tool("propose_wakeup", "card", "Propose an extra wakeup for the assistant (the owner approves it; it spends the allowance).",
         {"at": {"type": "string", "description": "ISO time; omit for as soon as possible"}, "budget": N, "reason": P}, ("budget", "reason"),
         propose_wakeup, "proposed a wakeup"),
    Tool("propose_level", "card", "Propose a different budget level (dormant, frugal, steady, busy, max); the owner decides.",
         {"level": P, "reason": P}, ("level",), propose_level, "proposed a level"),
    Tool("help", "read", "The built-in user guide to l0wagent: the web app's screens, how the assistant works, budgets, providers and models, "
         "chats, files and tools. No arguments lists the topics; topic=… reads one; query=… searches. Use it before explaining how something works.",
         {"topic": P, "query": P}, (), help_, "read the guide"),
    Tool("read_settings", "read", "The owner's settings: budgets per provider (level, limits, what they customized, period, caps, reply budget), "
         "level presets, the reserve, providers and their models, roles, the reviewer, standing instructions (the assistant's and the reviewer's), "
         "the advanced tuning knobs, notification choices. No secrets or keys.",
         {}, (), read_settings, "read the settings"),
    Tool("propose_settings", "card", "Propose settings changes; the owner sees each one (before → after) on a card and approves or declines. "
         "changes: [{setting, value, provider?}] with setting one of: level, allowance, allowed_hours, max_per_day, min_gap_minutes, max_wakeup, "
         "tiers, fallback, sweep (a level's limits; null resets one), reply_pts, fallback_pts, period (week|month), reset_day, caps ({five_hour, week} "
         "shares), bonus_pts (points to add to this period's allowance, e.g. after the owner used a usage reset), reset_spend (true: count "
         "the assistant's allowance spend from now; both lapse at the reset), models (an opencode provider's enabled model ids, in order), "
         "role.<worker|reviewer|replier|chat|utility> ({provider, tier}), "
         "reviewer.<mode|provider|tier|interval_minutes|budget_share|change_tier>, reserve_weekly_pct, reserve_five_hour_pct, instructions "
         "(full text, for the assistant), reviewer_instructions (full text, only the reviewer sees it), advanced.<KEY> (Settings → Advanced: "
         "e.g. advanced.HOLD_MINUTES, how long a run waits for the owner's reply; read_settings lists every key with its range), "
         "notifications.<event> (true/false), notifications.quiet ({start, end} HH:MM or null). provider defaults to the worker's. "
         "Read the settings first and explain the trade-off.",
         {"changes": {"type": "array", "items": {"type": "object"}}, "reason": P}, ("changes",), propose_settings, "proposed settings"),
    Tool("propose_handoff", "card", "Propose handing this chat's work to the assistant (the owner approves). target: new_thread (title, optional "
         "project slug), new_project (project_name and the first thread's title) or existing_thread (thread_id). mode: transcript (the whole chat "
         "is attached, with your message on top) or message (only your hand-off message: self-contained, with what to do and what's known). "
         "Suggest it when the work needs the assistant's computer, time, or memory.",
         {"target": P, "mode": P, "message": P, "title": P, "project": P, "project_name": P, "thread_id": N}, ("target", "mode", "message"),
         propose_handoff, "proposed a hand-off"),
    Tool("propose_egress_rule", "card", "Propose allowing (or blocking) a host for the assistant's internet access; the owner decides.",
         {"host": P, "action": P, "reason": P}, ("host",), propose_egress_rule, "proposed a network rule"),
]
BY_NAME = {t.name: t for t in TOOLS}
SCOPE_KINDS = {"read": {"read"}, "write": {"read", "write"}, "wake": {"read", "wake"}}


def run(ctx: Ctx, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool: (result text, is_error). Errors are returned, not raised: the model reads them."""
    tool = BY_NAME.get(name)
    if not tool:
        return f"No tool named {name}.", True
    if not isinstance(args, dict):
        return "Tool input must be an object.", True
    unknown = set(args) - set(tool.params)
    if unknown:
        return f"Unknown arguments: {', '.join(sorted(unknown))}.", True
    try:
        text, err = tool.fn(ctx, args), False
    except (BadRequest, Conflict) as e:
        text, err = f"{getattr(e, 'detail', None) or e}", True
    except NotFound as e:
        text, err = f"Not found: {e}", True
    except Exception as e:  # noqa: BLE001 - the model reads the failure; the chat goes on
        text, err = f"The tool failed: {type(e).__name__}: {str(e)[:300]}", True
    # Every caller (the chat loop, the sidecar, the connector) gets the redacted text.
    return (store_text(ctx.be, text) if ctx.be is not None else text), err


def summary(names: list[str]) -> str:
    """'Read 3 threads · searched' for the chat's tool line."""
    out: list[str] = []
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    for n, c in counts.items():
        label = BY_NAME[n].label if n in BY_NAME else n
        if c > 1 and label.startswith("read a "):
            label = f"read {c} {label[7:]}s"
        elif c > 1:
            label = f"{label} ×{c}"
        out.append(label)
    text = " · ".join(out)
    return text[:1].upper() + text[1:]
