"""The 1:1 chat (M6): a normal, instant chat with the owner's own model that knows the
assistant through tools (`tools.py`).

**Where the model call runs.** For key providers control runs the tool loop itself; the
tools are plain functions, so there's no shell anywhere:
- `anthropic-api`: the Messages API through the Anthropic SDK (streaming, adaptive thinking,
  the tier's effort, server-side refusal fallbacks). The provider's own content blocks are
  stored and replayed unchanged, thinking included.
- `opencode-go`: Go's OpenAI-compatible endpoint (its models aren't Claude).
- The Claude subscription's sanctioned client is Claude Code, a CLI with built-in shell tools,
  so it doesn't run in control: that goes through the stateless chat-runtime sidecar (later
  in M6). Until then a chat on the subscription says so.
- `L0W_CHAT_FAKE=1` swaps every provider for a scripted model (tests, the e2e stack).

**Usage** is the owner's own (`who='chat'` in the ledger, with the chat's id): it doesn't
touch the assistant's allowance or reply budget and isn't gated by the reserve. An optional
weekly cap per provider (`chat_cap` in the provider's config) stops the chat when reached.

**Cards**: tools can only propose sensitive things. The owner acts on the card in the chat
(save a secret, approve a wakeup, a level, a network rule); a note tells the model the outcome
(never a secret's value).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from . import tools as T
from .api import schemas as s
from .auth import iso, utcnow
from .backends.base import BadRequest, Conflict, NotFound
from .budgets import API, OPENCODE_GO, SUBSCRIPTION, price
from .db import Database

log = logging.getLogger("l0w.chat")
PROMPT = Path(__file__).parent / "prompts" / "chat.md"
MAX_STEPS = 12
MAX_TOKENS = 32_000
DELTA_EVERY_S = 0.06
ANTHROPIC_MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}  # server-side refusal fallbacks ("default" routing)
GO_BASE = os.environ.get("L0W_GO_BASE", "https://opencode.ai/inference/go/openai/v1")
CATALOG_URL = "https://models.dev/api.json"
GO_FALLBACK_PRICE = (1.0, 4.0)  # $/M tokens when the catalog can't be read: on the safe side


class ChatError(Exception):
    pass


@dataclass
class Turn:
    text: str = ""
    calls: list[dict] = field(default_factory=list)   # [{"id", "name", "input"}]
    raw: list[dict] | None = None                      # anthropic: content blocks to replay
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    model: str | None = None
    stop: str | None = None


def _strip(prompt: str) -> str:
    return "\n".join(line for line in prompt.splitlines() if not line.startswith("<!--")).strip()


# ── model clients ─────────────────────────────────────────────────────────
class AnthropicClient:
    """Messages API through the SDK: a manual streaming tool loop, one call per step."""

    def __init__(self, key: str, family: str, effort: str | None):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=key, max_retries=2)
        self.model = ANTHROPIC_MODELS.get(family, ANTHROPIC_MODELS["sonnet"])
        self.effort = effort if effort in ("low", "medium", "high", "xhigh", "max") else "medium"

    @staticmethod
    def history(rows: list[dict]) -> list[dict]:
        msgs: list[dict] = []

        def add(role: str, blocks: list[dict]) -> None:
            if not blocks:
                return
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"].extend(blocks)
            else:
                msgs.append({"role": role, "content": list(blocks)})

        for r in rows:
            if r["role"] == "owner":
                add("user", [{"type": "text", "text": r["body"]}])
            elif r["role"] == "system":
                add("user", [{"type": "text", "text": f"[control] {r['body']}"}])
            elif r["role"] == "assistant":
                raw = json.loads(r["raw_json"]) if r["raw_json"] and r["provider"] == API else None
                if raw:
                    add("assistant", raw)
                else:
                    blocks = [{"type": "text", "text": r["body"]}] if r["body"].strip() else []
                    blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]} for c in r["data"].get("calls", [])]
                    add("assistant", blocks)
            elif r["role"] == "tool":
                d = r["data"]
                add("user", [{"type": "tool_result", "tool_use_id": d["call_id"], "content": d.get("result") or "", "is_error": bool(d.get("is_error"))}])
        return msgs

    async def turn(self, system: str, rows: list[dict], on_text) -> Turn:
        tools = [{"name": t.name, "description": t.description, "input_schema": t.schema(), "eager_input_streaming": True} for t in T.TOOLS]
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=MAX_TOKENS, system=system, messages=self.history(rows), tools=tools,
                                      output_config={"effort": self.effort})
        if self.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        out = Turn(model=self.model)
        try:
            async with self.client.beta.messages.stream(**kwargs) as stream:
                async for ev in stream:
                    if ev.type == "text":
                        out.text += ev.text
                        on_text(out.text)
                msg = await stream.get_final_message()
        except ValueError as e:  # a tool input the SDK couldn't parse at all
            raise ChatError(f"The model sent a malformed tool call ({e}); try again.") from e
        except self.anthropic.APIStatusError as e:
            raise ChatError(f"Anthropic API error {e.status_code}: {str(e.message)[:300]}") from e
        except self.anthropic.APIConnectionError as e:
            raise ChatError("Couldn't reach the Anthropic API.") from e
        out.stop = msg.stop_reason
        out.model = msg.model or self.model
        out.raw = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
        out.text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "refusal":
            out.text = out.text or "(The model declined to answer this.)"
        elif msg.stop_reason != "max_tokens":
            out.calls = [{"id": b.id, "name": b.name, "input": b.input} for b in msg.content if b.type == "tool_use"]
        u = msg.usage
        out.usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                     "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0, "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        out.cost_usd = price(out.model, out.usage)
        return out


class OpenAICompatClient:
    """opencode Go's OpenAI-compatible chat completions, streamed."""

    def __init__(self, key: str, model: str, variant: str | None, prices: tuple[float, float], session: str, base: str = GO_BASE):
        self.key, self.model, self.variant, self.prices, self.base = key, model.split("/", 1)[-1], variant, prices, base.rstrip("/")
        # Go asks clients to name themselves and send one stable session id per conversation
        # (routing and prompt caching); see opencode.ai/v2/docs/console/go, "Where can I use it?".
        self.headers = {"Authorization": f"Bearer {key}", "User-Agent": f"l0wagent/{__version__}", "x-opencode-session": session}

    @staticmethod
    def history(system: str, rows: list[dict]) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system}]
        for r in rows:
            if r["role"] == "owner":
                msgs.append({"role": "user", "content": r["body"]})
            elif r["role"] == "system":
                msgs.append({"role": "user", "content": f"[control] {r['body']}"})
            elif r["role"] == "assistant":
                m: dict[str, Any] = {"role": "assistant", "content": r["body"] or None}
                calls = r["data"].get("calls", [])
                if calls:
                    m["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["input"])}} for c in calls]
                msgs.append(m)
            elif r["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": r["data"]["call_id"], "content": r["data"].get("result") or ""})
        return msgs

    async def turn(self, system: str, rows: list[dict], on_text) -> Turn:
        body: dict[str, Any] = {"model": self.model, "messages": self.history(system, rows), "stream": True, "stream_options": {"include_usage": True},
                                "tools": [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema()}} for t in T.TOOLS]}
        if self.variant:
            body["reasoning_effort"] = self.variant
        out = Turn(model=f"{OPENCODE_GO}/{self.model}")
        calls: dict[int, dict] = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as c, \
                    c.stream("POST", f"{self.base}/chat/completions", json=body, headers=self.headers) as r:
                if r.status_code != 200:
                    raise ChatError(f"opencode Go error {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        out.usage = {"input_tokens": chunk["usage"].get("prompt_tokens", 0), "output_tokens": chunk["usage"].get("completion_tokens", 0)}
                    for ch in chunk.get("choices") or []:
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            out.text += d["content"]
                            on_text(out.text)
                        for tc in d.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if ch.get("finish_reason"):
                            out.stop = ch["finish_reason"]
        except httpx.HTTPError as e:
            raise ChatError(f"Couldn't reach opencode Go: {e}") from e
        for i, cslot in sorted(calls.items()):
            try:
                args = json.loads(cslot["args"] or "{}")
            except json.JSONDecodeError:
                args = {"__invalid__": cslot["args"][:200]}
            out.calls.append({"id": cslot["id"] or f"call_{i}", "name": cslot["name"], "input": args})
        pin, pout = self.prices
        out.cost_usd = (out.usage.get("input_tokens", 0) * pin + out.usage.get("output_tokens", 0) * pout) / 1e6
        return out


class FakeClient:
    """Scripted, for tests: 'fake:tools' reads threads, 'fake:handoff' starts a thread,
    'fake:secret' asks for a secret, 'fake:fail' errors; otherwise it echoes."""

    def __init__(self, model: str):
        self.model = model

    async def turn(self, system: str, rows: list[dict], on_text) -> Turn:
        last_owner = next((r["body"] for r in reversed(rows) if r["role"] == "owner"), "")
        after_tool = rows and rows[-1]["role"] in ("tool", "system")
        out = Turn(model=self.model, usage={"input_tokens": 2000, "output_tokens": 120}, cost_usd=0.25)
        if "fake:fail" in last_owner:
            raise ChatError("Fake failure (fake:fail).")
        if not after_tool and "fake:tools" in last_owner:
            out.calls = [{"id": f"c{time.time_ns()}", "name": "list_threads", "input": {}}, {"id": f"c{time.time_ns() + 1}", "name": "status", "input": {}}]
            return out
        if not after_tool and "fake:handoff" in last_owner:
            out.text = "I'll hand that to the assistant."
            out.calls = [{"id": f"c{time.time_ns()}", "name": "create_thread", "input": {"title": "Water the tomatoes", "message": "Please set up a watering reminder for the tomatoes."}}]
            return out
        if not after_tool and "fake:secret" in last_owner:
            out.calls = [{"id": f"c{time.time_ns()}", "name": "ask_for_secret",
                          "input": {"name": "GITHUB_TOKEN", "description": "A fine-grained token with read access to your repos", "help_url": "https://github.com/settings/tokens"}}]
            return out
        if after_tool:
            last = rows[-1]
            text = f"Done: {T.summary([last['data'].get('name', '')])}." if last["role"] == "tool" else f"Noted: {last['body']}"
        else:
            text = f"You said: {last_owner}"
        for i in range(0, len(text), 8):
            out.text = text[: i + 8]
            on_text(out.text)
            await asyncio.sleep(0.01)
        out.text = text
        return out


# ── the chats ─────────────────────────────────────────────────────────────
class Chats:
    def __init__(self, db: Database, be, live, budgets, vault):
        self.db, self.be, self.live, self.budgets, self.vault = db, be, live, budgets, vault
        self.tasks: dict[int, asyncio.Task] = {}
        self.fake = os.environ.get("L0W_CHAT_FAKE") == "1"

    def startup(self) -> None:
        self.db.ex("UPDATE chats SET busy=0 WHERE busy=1")  # a reply cut off by a restart

    # rows and models
    def _row(self, chat_id: int) -> dict:
        row = self.db.q1("SELECT * FROM chats WHERE id=?", (chat_id,))
        if not row:
            raise NotFound(f"chat {chat_id}")
        return row

    def _msg(self, r: dict) -> s.ChatMessage:
        return s.ChatMessage(id=r["id"], chat_id=r["chat_id"], role=r["role"], body=r["body"], data=json.loads(r["data_json"] or "{}"),
                             model=r["model"], created_at=r["created_at"])

    def chat(self, chat_id: int) -> s.Chat:
        r = self._row(chat_id)
        last = self.db.q1("SELECT body FROM chat_messages WHERE chat_id=? AND role IN ('owner','assistant') AND body != '' ORDER BY id DESC LIMIT 1", (chat_id,))
        handed = None
        if r["handed_off_thread_id"]:
            t = self.db.q1("SELECT id, title FROM threads WHERE id=?", (r["handed_off_thread_id"],))
            handed = s.ThreadRef(id=t["id"], title=t["title"]) if t else None
        return s.Chat(id=r["id"], title=r["title"], provider=r["provider"], tier=r["tier"], status=r["status"], busy=bool(r["busy"]),
                      handed_off_to=handed, context_threads=json.loads(r["context_thread_ids"] or "[]"), last_message_preview=(last or {}).get("body", "")[:140] or None,
                      created_at=r["created_at"], updated_at=r["updated_at"], available=self.available(r["provider"]) is None)

    def chats(self, archived: bool = False) -> list[s.Chat]:
        rows = self.db.q("SELECT id FROM chats WHERE status=? ORDER BY updated_at DESC, id DESC LIMIT 200", ("archived" if archived else "open",))
        return [self.chat(r["id"]) for r in rows]

    def messages(self, chat_id: int) -> list[s.ChatMessage]:
        self._row(chat_id)
        return [self._msg(r) for r in self.db.q("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", (chat_id,))]

    def available(self, provider: str) -> str | None:
        """None if a chat can run on this provider, else why not."""
        if self.fake:
            return None
        if provider not in self.budgets.provider_ids():
            return "That provider is off or removed."
        kind = self.budgets.kind(provider)
        if kind == SUBSCRIPTION:
            return ("Chats on the Claude subscription need the chat sidecar, which isn't installed yet. "
                    "Add an Anthropic API key or opencode Go (Settings → Providers) and pick it for the chat role.")
        if not self.budgets.credential(provider):
            return "No key is saved for that provider."
        return None

    def _touch(self, chat_id: int) -> None:
        self.db.ex("UPDATE chats SET updated_at=? WHERE id=?", (self.db.now(), chat_id))
        self.live.emit("chat", {"chat_id": chat_id})

    def _add(self, chat_id: int, role: str, body: str = "", data: dict | None = None, raw: list | None = None, provider: str | None = None,
             model: str | None = None, key: str | None = None) -> int:
        mid = self.db.ex("INSERT INTO chat_messages(chat_id, role, body, data_json, raw_json, provider, model, idempotency_key, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (chat_id, role, self.vault.redact(body) or "", self.vault.redact(json.dumps(data or {})), self.vault.redact(json.dumps(raw)) if raw else None,
                          provider, model, key, self.db.now()))
        self._touch(chat_id)
        return mid

    # owner actions
    def create(self, req: s.ChatCreate) -> s.Chat:
        if req.idempotency_key:
            old = self.db.q1("SELECT chat_id FROM chat_messages WHERE idempotency_key=?", (req.idempotency_key,))
            if old:
                return self.chat(old["chat_id"])
        role = self.budgets.role("chat")
        provider, tier = req.provider or role.provider, req.tier or role.tier
        if provider not in self.budgets.provider_ids(enabled_only=False):
            raise BadRequest("bad_provider", f"Unknown provider {provider}.")
        title = (req.title or req.message.strip().splitlines()[0] if req.message.strip() else "New chat")[:80]
        now = self.db.now()
        cid = self.db.ex("INSERT INTO chats(title, provider, tier, created_at, updated_at) VALUES(?,?,?,?,?)", (self.vault.redact(title), provider, tier, now, now))
        self.send(cid, s.ChatMessageCreate(body=req.message, idempotency_key=req.idempotency_key))
        return self.chat(cid)

    def patch(self, chat_id: int, req: s.ChatPatch) -> s.Chat:
        self._row(chat_id)
        if req.title:
            self.db.ex("UPDATE chats SET title=? WHERE id=?", (self.vault.redact(req.title.strip())[:80], chat_id))
        if req.provider:
            if req.provider not in self.budgets.provider_ids():
                raise BadRequest("bad_provider", f"Unknown provider {req.provider}.")
            self.db.ex("UPDATE chats SET provider=? WHERE id=?", (req.provider, chat_id))
        if req.tier:
            self.db.ex("UPDATE chats SET tier=? WHERE id=?", (req.tier, chat_id))
        if req.status:
            self.db.ex("UPDATE chats SET status=? WHERE id=?", (req.status, chat_id))
        self._touch(chat_id)
        return self.chat(chat_id)

    def send(self, chat_id: int, req: s.ChatMessageCreate) -> s.ChatMessage:
        row = self._row(chat_id)
        if req.idempotency_key:
            old = self.db.q1("SELECT * FROM chat_messages WHERE idempotency_key=?", (req.idempotency_key,))
            if old:
                return self._msg(old)
        if row["busy"]:
            raise Conflict("busy", "The chat is still replying.")
        why = self.available(row["provider"])
        mid = self._add(chat_id, "owner", req.body, key=req.idempotency_key)
        if why:
            self._add(chat_id, "system", why)
        else:
            self._respond_soon(chat_id)
        return self._msg(self.db.q1("SELECT * FROM chat_messages WHERE id=?", (mid,)))  # type: ignore[arg-type]

    def _respond_soon(self, chat_id: int) -> None:
        self.db.ex("UPDATE chats SET busy=1 WHERE id=?", (chat_id,))
        loop = self.be.loop
        if loop is None:
            return
        loop.call_soon_threadsafe(lambda: self.tasks.__setitem__(chat_id, loop.create_task(self._respond(chat_id))))

    def stop(self, chat_id: int) -> s.Chat:
        task = self.tasks.get(chat_id)
        if task and not task.done() and self.be.loop:
            self.be.loop.call_soon_threadsafe(task.cancel)
        self.db.ex("UPDATE chats SET busy=0 WHERE id=?", (chat_id,))
        self._touch(chat_id)
        return self.chat(chat_id)

    # the loop
    def _client(self, row: dict):
        provider, tier = row["provider"], row["tier"]
        spec = self.budgets.runtime_spec(provider, tier)
        if self.fake:
            return FakeClient(spec["model"]), provider
        kind = self.budgets.kind(provider)
        key = self.budgets.credential(provider) or ""
        if kind == API:
            fam, _, effort = tier.partition("/")
            return AnthropicClient(key, fam or "sonnet", effort or None), provider
        if kind == OPENCODE_GO:
            return OpenAICompatClient(key, spec["model"], spec["effort"], self._go_prices(spec["model"]), self._session_id(row["id"])), provider
        raise ChatError(self.available(provider) or "This provider can't run chats.")

    def _session_id(self, chat_id: int) -> str:
        """A stable, opaque id per chat (no instance or owner details)."""
        import hashlib

        return "l0w-" + hashlib.sha256(f"{self.vault.key!r}:chat:{chat_id}".encode()).hexdigest()[:32]

    def _go_prices(self, model: str) -> tuple[float, float]:
        """opencode Go's catalog price for a model ($/M in, out), from models.dev, cached a day."""
        row = self.db.q1("SELECT value, updated_at FROM kv WHERE key='go_catalog'")
        cat = None
        if row:
            cat = json.loads(row["value"])
            if datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")) < utcnow() - timedelta(days=1):
                cat = None if not cat else cat  # stale but usable; refresh below
                row = None
        if row is None:
            try:
                r = httpx.get(CATALOG_URL, timeout=10, headers={"User-Agent": "l0wagent"})
                go = r.json().get(OPENCODE_GO, {}).get("models", {})
                cat = {k: [v.get("cost", {}).get("input", 0), v.get("cost", {}).get("output", 0)] for k, v in go.items()}
                self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES('go_catalog', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                           (json.dumps(cat), self.db.now()))
            except (httpx.HTTPError, ValueError):
                pass
        p = (cat or {}).get(model.split("/", 1)[-1])
        return (float(p[0]), float(p[1])) if p else GO_FALLBACK_PRICE

    def _history(self, chat_id: int) -> list[dict]:
        rows = []
        for r in self.db.q("SELECT * FROM chat_messages WHERE chat_id=? AND role IN ('owner','assistant','tool','system') ORDER BY id", (chat_id,)):
            rows.append({**r, "data": json.loads(r["data_json"] or "{}")})
        # A reply cut off between calling tools and their results: answer the missing calls.
        out: list[dict] = []
        for i, r in enumerate(rows):
            out.append(r)
            if r["role"] == "assistant":
                have = {x["data"].get("call_id") for x in rows[i + 1:] if x["role"] == "tool"}
                for c in r["data"].get("calls", []):
                    if c["id"] not in have:
                        out.append({"role": "tool", "body": "", "data": {"call_id": c["id"], "name": c["name"], "result": "(not run: the reply was interrupted)", "is_error": True},
                                    "raw_json": None, "provider": None})
        return out

    def _system(self, row: dict) -> str:
        text = _strip(PROMPT.read_text())
        now = self.budgets.local(utcnow())
        text += f"\n\nNow: {now:%a %Y-%m-%d %H:%M} ({self.budgets.tz()})."
        ctx = json.loads(row["context_thread_ids"] or "[]")
        if ctx:
            text += "\n\nThe owner pulled these threads into this chat as context (read them with read_thread): " + ", ".join(f"#{t}" for t in ctx) + "."
        return text

    def _over_cap(self, provider: str) -> str | None:
        cap = (self.budgets.config(provider) or {}).get("chat_cap")
        if not cap:
            return None
        start, end = self.budgets.week(provider)
        used = self.budgets.spent(provider, start, end, who=("chat",))
        return f"This week's chat cap on {provider} ({cap:g} pts) is used up ({used:.1f} pts)." if used >= cap else None

    def _count(self, chat_id: int, provider: str, turn: Turn) -> None:
        pts = turn.cost_usd * self.budgets.pts_per_usd(provider)
        now = self.db.now()
        u = turn.usage
        self.db.ex("INSERT INTO usage_ledger(provider, who, bucket, chat_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                   "cost_usd, cost_final, pts, at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                   (provider, "chat", "chat", chat_id, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0),
                    u.get("cache_creation_input_tokens", 0), turn.cost_usd, pts, now, now))
        self.live.emit("budget")

    def _streamer(self, chat_id: int):
        """A callback that sends the reply so far as `chat_delta` events, at most every DELTA_EVERY_S."""
        last = [0.0]

        def on_text(text: str) -> None:
            t = time.monotonic()
            if t - last[0] >= DELTA_EVERY_S:
                last[0] = t
                self.live.emit("chat_delta", {"chat_id": chat_id, "text": self.vault.redact(text)})
        return on_text

    async def _respond(self, chat_id: int) -> None:
        try:
            row = self._row(chat_id)
            client, provider = self._client(row)
            system = self._system(row)
            for _ in range(MAX_STEPS):
                cap = self._over_cap(provider)
                if cap:
                    self._add(chat_id, "system", cap)
                    return
                turn = await client.turn(system, self._history(chat_id), self._streamer(chat_id))
                self._count(chat_id, provider, turn)
                calls = [{"id": c["id"], "name": c["name"], "input": c["input"]} for c in turn.calls]
                if turn.text.strip() or calls:
                    self._add(chat_id, "assistant", turn.text, {"calls": calls, "stop": turn.stop}, raw=turn.raw, provider=provider, model=turn.model)
                if not calls:
                    return
                ctx = T.Ctx(be=self.be, chat_id=chat_id)
                for c in calls:
                    result, is_error = await asyncio.to_thread(T.run, ctx, c["name"], c["input"])
                    self._add(chat_id, "tool", "", {"call_id": c["id"], "name": c["name"], "input": c["input"], "result": result, "is_error": is_error})
                for card in ctx.cards:
                    self._add(chat_id, "card", "", card)
        except asyncio.CancelledError:
            self._add(chat_id, "system", "Stopped.")
            raise
        except ChatError as e:
            self._add(chat_id, "system", str(e))
        except Exception as e:  # noqa: BLE001 - the chat must always end its turn
            log.exception("chat %s failed", chat_id)
            self._add(chat_id, "system", f"The chat failed: {type(e).__name__}: {str(e)[:200]}")
        finally:
            self.db.ex("UPDATE chats SET busy=0 WHERE id=?", (chat_id,))
            self.live.emit("chat_delta", {"chat_id": chat_id, "text": None})
            self._touch(chat_id)

    # cards
    def decide(self, chat_id: int, message_id: int, req: s.ChatCardDecision) -> s.ChatMessage:
        self._row(chat_id)
        r = self.db.q1("SELECT * FROM chat_messages WHERE id=? AND chat_id=? AND role='card'", (message_id, chat_id))
        if not r:
            raise NotFound(f"card {message_id}")
        card = json.loads(r["data_json"])
        if card.get("state") != "open":
            raise Conflict("decided", "This card was already decided.")
        kind = card["kind"]
        if req.decision == "decline":
            card["state"] = "declined"
            note = f"The owner declined the {kind} card."
        elif kind == "secret":
            if not req.value:
                raise BadRequest("no_value", "Enter the value.")
            self.be.set_secret(card["name"], s.SecretSet(value=req.value, description=card.get("description") or None))
            card["state"] = "done"
            note = f"The owner saved the secret {card['name']} (the value is stored write-only; you can't see it)."
        elif kind == "wakeup":
            at = datetime.fromisoformat(card["at"]) if card.get("at") else utcnow() + timedelta(minutes=2)
            w = self.be.create_wakeup(s.WakeupCreate(at=at, budget=card["budget"], reason=card.get("reason") or "From a chat"))
            card.update(state="done", wakeup_id=w.id)
            note = f"The owner approved a wakeup at {iso(w.at)} with {card['budget']} pts."
        elif kind == "level":
            self.be.patch_budget(self.budgets.role("worker").provider, s.BudgetPatch(level=card["level"]))
            card["state"] = "done"
            note = f"The owner set the level to {card['level']}."
        elif kind == "egress":
            add = self.be.conv.add_egress_rule
            if add is None:
                raise Conflict("no_egress", "Egress isn't configured.")
            add(card["host"], card.get("action", "allow"), f"chat #{chat_id}")
            card["state"] = "done"
            note = f"The owner added the network rule: {card.get('action', 'allow')} {card['host']}."
        else:
            raise BadRequest("bad_card", f"Unknown card kind {kind}.")
        self.db.ex("UPDATE chat_messages SET data_json=? WHERE id=?", (json.dumps(card), message_id))
        self._add(chat_id, "system", note)
        return self._msg(self.db.q1("SELECT * FROM chat_messages WHERE id=?", (message_id,)))  # type: ignore[arg-type]

    # hand-off
    def _transcript(self, chat_id: int) -> str:
        lines = [f"# Chat: {self._row(chat_id)['title']}", ""]
        for r in self.db.q("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", (chat_id,)):
            if r["role"] == "owner":
                lines += [f"**Owner:** {r['body']}", ""]
            elif r["role"] == "assistant" and r["body"].strip():
                lines += [f"**Chat:** {r['body']}", ""]
            elif r["role"] == "tool":
                d = json.loads(r["data_json"])
                lines += [f"_(tool {d.get('name')})_", ""]
            elif r["role"] == "system":
                lines += [f"_{r['body']}_", ""]
        return "\n".join(lines)

    def handoff(self, chat_id: int, req: s.ChatHandoff) -> s.Chat:
        row = self._row(chat_id)
        transcript = self._transcript(chat_id)
        up = self.be.upload(req.project, f"chat-{chat_id}.md", transcript.encode(), "text/markdown", None)
        summary = (req.summary or "").strip() or self._auto_summary(chat_id)
        t = self.be.conv.create_thread(req.title or row["title"], req.project, "chat")
        self.be.conv.post_owner_message(t.id, f"{summary}\n\n(Handed over from a 1:1 chat; the transcript is attached.)", [up.id], None, via=f"chat:{chat_id}")
        self.db.ex("UPDATE chats SET handed_off_thread_id=? WHERE id=?", (t.id, chat_id))
        self._add(chat_id, "system", f"Continued in the thread #{t.id} “{t.title}”.", {"thread_id": t.id})
        return self.chat(chat_id)

    def _auto_summary(self, chat_id: int) -> str:
        owner = [r["body"] for r in self.db.q("SELECT body FROM chat_messages WHERE chat_id=? AND role='owner' ORDER BY id", (chat_id,))]
        last = self.db.q1("SELECT body FROM chat_messages WHERE chat_id=? AND role='assistant' AND body!='' ORDER BY id DESC LIMIT 1", (chat_id,))
        text = "What I asked in the chat:\n" + "\n".join(f"- {b[:400]}" for b in owner[-6:])
        if last:
            text += f"\n\nWhere the chat got to:\n{last['body'][:1500]}"
        return text

    def ask(self, chat_id: int, req: s.ChatAsk) -> s.ChatMessage:
        self._row(chat_id)
        m = self.be.conv.post_owner_message(req.thread_id, req.message, [], None, via=f"chat:{chat_id}")
        t = self.be.thread(req.thread_id)
        mid = self._add(chat_id, "system", f"Asked the assistant in #{t.id} “{t.title}”.", {"thread_id": t.id, "message_id": m.id})
        return self._msg(self.db.q1("SELECT * FROM chat_messages WHERE id=?", (mid,)))  # type: ignore[arg-type]

    def pull(self, chat_id: int, thread_id: int) -> s.Chat:
        row = self._row(chat_id)
        t = self.be.thread(thread_id)
        ids = json.loads(row["context_thread_ids"] or "[]")
        if t.id not in ids:
            ids.append(t.id)
            self.db.ex("UPDATE chats SET context_thread_ids=? WHERE id=?", (json.dumps(ids), chat_id))
            self._add(chat_id, "system", f"The owner pulled the thread #{t.id} “{t.title}” into this chat as read-only context.", {"thread_id": t.id})
        return self.chat(chat_id)
