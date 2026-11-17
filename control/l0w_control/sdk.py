"""The extension SDK, version 1: everything an extension's Python code may use.

An extension's entry module defines `setup(ext: Extension) -> None`. It receives one `Extension`
and registers what it adds through it. It never imports the rest of `l0w_control` and never opens the
database: upstream changes to control's internals stay invisible to it, and the security invariants
hold because every write goes through methods that apply them (redaction, containment, owner-only
decisions). The reference is docs/EXTENDING.md; this module is the contract it documents.

    from fastapi import APIRouter
    from l0w_control.sdk import Extension

    def setup(ext: Extension) -> None:
        @ext.router.get("/plants")             # GET /api/ext/<name>/plants, owner only
        def plants():
            return ext.store.get("plants", [])

        @ext.agent_router.post("/log")         # `assistant ext <name> log ...`, from the sandbox
        def log(body: dict):
            ...

        ext.on("run_ended", lambda event: ...)
        ext.every(3600, lambda: ...)
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from .extensions import Extensions

SDK_VERSION = "1.0"

# Events an extension can listen to with ext.on(...). Each handler gets one dict.
EVENTS = {
    "run_started": "a run started: {run_id, kind, why, thread_id, provider}",
    "run_ended": "a run ended: {run_id, status, result}",
    "wakeup_planned": "a wakeup was planned: {wakeup_id, at, budget, tier, reason, by}",
    "message_posted": "a message was posted in a thread: {message_id, thread_id, sender, body}",
    "ask_decided": "the owner decided an ask: {kind, id, decision}",
    "file_shelved": "a file was put on a project's shelf: {shelf_item_id, project, volume, path}",
    "extension_card_resolved": "the owner resolved one of this extension's cards: {card_id, kind, action, data (the owner's), card (the card's own data)}",
}

# Prompt slots an extension may add a fragment to (manifest [prompts]).
PROMPT_SLOTS = ("system", "wakeup", "reviewer", "chat")


class ExtensionError(Exception):
    """Raised by SDK calls an extension may not make (or made wrongly); shown on the Extensions page."""


class HubUnavailable(ExtensionError):
    """ext.hub calls need a joined hub; until there is one, every call raises this."""


@dataclass
class Store:
    """The extension's own key-value store (JSON values). Nothing else in the database is the extension's."""

    _x: Extensions
    _name: str

    def get(self, key: str, default: Any = None) -> Any:
        return self._x.kv_get(self._name, "data:" + key, default)

    def set(self, key: str, value: Any) -> None:
        self._x.kv_set(self._name, "data:" + key, value)

    def delete(self, key: str) -> None:
        self._x.kv_delete(self._name, "data:" + key)

    def keys(self, prefix: str = "") -> list[str]:
        return [k[5:] for k in self._x.kv_keys(self._name, "data:" + prefix)]


@dataclass
class SettingsView:
    """The settings the manifest declares under [settings]; the owner edits them in Settings → Extensions."""

    _x: Extensions
    _name: str

    def get(self, key: str) -> Any:
        return self._x.setting(self._name, key)

    def all(self) -> dict[str, Any]:
        return self._x.settings_values(self._name)


@dataclass
class Threads:
    """Threads and messages. Writes are always labeled as coming from the extension; they never act as the owner."""

    _x: Extensions
    _name: str

    def list(self, project: str | None = None, archived: bool = False) -> list[dict]:
        return [t.model_dump(mode="json") for t in self._x.be.conv.threads(project, project is None, archived)]

    def get(self, thread_id: int) -> dict:
        return self._x.be.conv.thread(thread_id).model_dump(mode="json")

    def messages(self, thread_id: int, limit: int = 50) -> list[dict]:
        rows = self._x.be.db.q("SELECT id, sender, body, created_at FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, limit))
        return list(reversed(rows))

    def create(self, title: str, project: str | None = None, type: str | None = None, first_note: str | None = None) -> dict:
        """A new thread. `type` must be one the manifest declares under [[ui.thread_types]] (or None for a normal thread)."""
        return self._x.create_thread(self._name, title, project, type, first_note)

    def note(self, thread_id: int, body: str) -> None:
        """A system note in a thread, prefixed with the extension's title. The agent reads it like any message."""
        self._x.note(self._name, thread_id, body)


@dataclass
class Runs:
    _x: Extensions

    def active(self) -> int | None:
        """The run in progress, if any (agent routes use it to tie a call to its run)."""
        return self._x.be.runs_mgr.active_run_id()

    def get(self, run_id: int) -> dict:
        return self._x.be.runs_mgr.run(run_id).model_dump(mode="json")

    def recent(self, limit: int = 20) -> list[dict]:
        return self._x.be.db.q("SELECT id, kind, why, status, provider, started_at, ended_at, result FROM runs ORDER BY id DESC LIMIT ?", (limit,))


@dataclass
class Projects:
    _x: Extensions

    def list(self) -> list[dict]:
        return [p.model_dump(mode="json") for p in self._x.be.conv.projects(None)]

    def get(self, slug: str) -> dict:
        return self._x.be.conv.project(slug).model_dump(mode="json")


@dataclass
class VaultView:
    """Whether a secret exists. Never its value: extensions don't read secrets."""

    _x: Extensions

    def has(self, name: str) -> bool:
        return self._x.be.vault.exists(name)


@dataclass
class Inbox:
    """Inbox cards of the extension's own kinds (manifest [[ui.inbox_cards]]). The owner resolves them; the
    extension hears it through ext.on("extension_card_resolved", ...)."""

    _x: Extensions
    _name: str

    def card(self, kind: str, title: str, data: dict | None = None, thread_id: int | None = None) -> int:
        return self._x.card_create(self._name, kind, title, data or {}, thread_id)

    def withdraw(self, card_id: int) -> None:
        self._x.card_withdraw(self._name, card_id)

    def open(self) -> list[dict]:
        return self._x.cards_view(self._name, "open")


@dataclass
class Folders:
    """Managed folders: /work/_inbox/_ext/<name>/<folder>/, declared in the manifest. The only place in the agent's
    storage control writes for an extension. Paths are relative to the folder; symlinks are never followed."""

    _x: Extensions
    _name: str

    def write(self, folder: str, path: str, data: bytes | str) -> str:
        return self._x.folder_write(self._name, folder, path, data.encode() if isinstance(data, str) else data)

    def read(self, folder: str, path: str) -> bytes:
        return self._x.folder_read(self._name, folder, path)

    def list(self, folder: str, path: str = "") -> list[str]:
        return self._x.folder_list(self._name, folder, path)

    def agent_path(self, folder: str) -> str:
        """Where the agent sees the folder (for messages and prompt fragments)."""
        return f"/work/_inbox/_ext/{self._name}/{folder}"


@dataclass
class Hub:
    """Calls to hub services through the gateway, with the instance's identity. The extension never sees
    the instance key. No hub exists yet: every call raises HubUnavailable."""

    _x: Extensions

    def available(self) -> bool:
        return False

    def call(self, service: str, method: str, payload: dict | None = None) -> dict:
        raise HubUnavailable("This instance hasn't joined a hub.")


@dataclass
class Extension:
    """What `setup(ext)` receives. Register everything during setup; nothing registers later."""

    name: str
    title: str
    version: str
    root: str                         # the extension's folder (read-only)
    router: APIRouter                 # owner routes: /api/ext/<name>/...
    agent_router: APIRouter           # agent routes: /api/agent/ext/<name>/...
    store: Store
    settings: SettingsView
    threads: Threads
    runs: Runs
    projects: Projects
    vault: VaultView
    inbox: Inbox
    folders: Folders
    hub: Hub
    _x: Extensions
    hooks: dict[str, list[Callable[[dict], Any]]] = field(default_factory=dict)
    jobs: list[tuple[float, Callable[[], Any]]] = field(default_factory=list)

    def on(self, event: str, handler: Callable[[dict], Any]) -> None:
        """Listen to an event (see EVENTS). Handlers run off the request path; an exception is recorded, not raised."""
        if event not in EVENTS:
            raise ExtensionError(f"Unknown event {event!r}. Known: {', '.join(EVENTS)}.")
        self.hooks.setdefault(event, []).append(handler)

    def every(self, seconds: float, job: Callable[[], Any]) -> None:
        """A background job, run every `seconds` (at least 30) in a worker thread while the extension is enabled."""
        if seconds < 30:
            raise ExtensionError("Background jobs run at most every 30 seconds.")
        self.jobs.append((float(seconds), job))

    def notify(self, title: str, body: str = "", url: str = "/") -> None:
        """A notification to the owner (push or ntfy, as they've set up)."""
        self._x.be.notifier.notify("extension", f"{self.title}: {title}", body, url, "extension", self.name)

    def emit(self, type: str, data: dict | None = None) -> None:
        """A live event for the extension's UI: it arrives in the web app as {ext, type, data}."""
        self._x.be.live.emit("extension", {"ext": self.name, "type": type, "data": data or {}})

    def log(self, text: str) -> None:
        self._x.log(self.name, text)

    def file(self, rel: str) -> str:
        """Read a text file from the extension's own folder (e.g. a template)."""
        path = os.path.realpath(os.path.join(self.root, rel))
        if not path.startswith(os.path.realpath(self.root) + os.sep):
            raise ExtensionError(f"{rel} is outside the extension")
        with open(path, encoding="utf-8") as f:
            return f.read()


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
