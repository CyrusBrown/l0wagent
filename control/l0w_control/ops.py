"""Control's side of the ops container (H6; docs/plans/hub-development.md, "The `ops` container: design").

The ops container does the privileged work control can't: git (installing an extension at a pinned commit) and, if the owner opted
in at install, updating the whole stack. Control has no Docker socket and never will; it asks ops through a fixed command API
(`L0W_OPS_URL`, `L0W_OPS_TOKEN`; ops/README.md). Without those two, every feature here hides.

- **Update now.** The owner confirms, control turns its drain on (no new runs start), waits for the assistant to be idle (the
  same `updates.blockers` `./l0w update` waits for, up to UPDATE_IDLE_WAIT_MINUTES), then asks ops to update. Ops pulls the
  images and starts the one-shot updater container, which replaces everything, control included, and leaves its result in
  `extensions/.l0w-update.json`. The new control reads it after the restart and shows success or failure once (a notification and
  the banner). The drain is cleared on any failure, and on success (the updater runs with --no-drain).
- **Extensions from a git URL.** Resolve a branch or tag to a commit, then install that commit; removing is only for what ops
  installed. Control rescans afterwards.
- **A hub service's extension.** Each service row that names an extension has its own switch. While it is on, the extension is
  installed at the commit the hub's signed service document lists (ops checks the document against the pinned hub key). When the
  hub lists a newer commit, it is applied automatically and logged, or, if the owner set this hub to ask first, an Inbox card
  (permission action `extension_update`, which only control can file) holds it until the owner agrees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import __version__
from .api import schemas as s
from .auth import iso, parse, utcnow
from .backends.base import BadRequest, Conflict, NotFound
from .updates import blockers, draining, set_drain

log = logging.getLogger("l0w.ops")

EXT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")  # the same rule as ops and the CLI
STATUS_EVERY = timedelta(minutes=5)
RESULT_SHOWN = timedelta(hours=24)
UPDATE_GIVE_UP = timedelta(minutes=45)  # from the start of the pull: the updater should be done long before this
KEY_STATE = "update_apply"
KEY_RESULT = "update_result"


class OpsError(Exception):
    """A refusal or failure from ops. `code` is ops' own error code (`not_in_toml`, `doc_mismatch`, `not_ours`, ...) or `ops_unreachable`."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status, self.code, self.detail = status, code, detail


class OpsClient:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.token, self.transport = url.rstrip("/"), token, transport

    async def call(self, method: str, path: str, body: dict | None = None, *, timeout: float = 30.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5), transport=self.transport) as c:
                r = await c.request(method, self.url + path, headers={"Authorization": f"Bearer {self.token}"}, **({"json": body} if body is not None else {}))
        except httpx.HTTPError as e:
            raise OpsError(0, "ops_unreachable", f"The ops container isn't answering ({e.__class__.__name__}).") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if r.status_code >= 400:
            raise OpsError(r.status_code, str(data.get("error") or f"ops_{r.status_code}"), str(data.get("detail") or ""))
        return data


def ext_name(address: str) -> str:
    """The name a hub service's extension installs under: the service's own name on the hub (`board` for `board@hub.example`)."""
    return address.split("@", 1)[0].lower()


def _norm_path(p: object) -> str | None:
    p = str(p or "").strip().strip("/")
    return None if p in ("", ".") else p


def _short(commit: str | None) -> str:
    return (commit or "")[:10]


class Ops:
    def __init__(self, db, live, settings, notifier, updates, membership, hubservices, client: OpsClient | None, *, now: Callable[[], datetime] = utcnow):
        self.db, self.live, self.settings, self.notifier, self.updates, self.m, self.h = db, live, settings, notifier, updates, membership, hubservices
        self.client, self.now = client, now
        self.poll = 5.0  # seconds between looks while an update waits or runs (tests make it tiny)
        self.result_file: Path | None = None  # extensions/.l0w-update.json, read from control's read-only extensions mount
        self.extensions = None  # set by the backend (extensions.Extensions)
        self.conv = None  # set by the backend (conv.Conv): files the extension-update card
        self.loop: asyncio.AbstractEventLoop | None = None
        self._status: dict | None = None
        self._status_at: datetime | None = None
        self._task: asyncio.Task | None = None  # the running update
        self._hub_tasks: set[asyncio.Task] = set()
        self._hub_lock = asyncio.Lock()
        self._hub_pending = False
        hubservices.ext_changed = self.schedule_hub
        membership.on_policy = self.schedule_hub

    # ── is it there, and what does it say ──────────────────────────────────
    @property
    def available(self) -> bool:
        return self.client is not None

    @property
    def docker(self) -> bool:
        return bool(self._status and self._status.get("docker"))

    async def refresh_status(self, force: bool = True) -> dict | None:
        if not self.client:
            return None
        if not force and self._status_at and self.now() - self._status_at < STATUS_EVERY:
            return self._status
        try:
            self._status = await self.client.call("GET", "/v0/status", timeout=10)
        except OpsError as e:
            log.info("ops status: %s", e)
            self._status = None
        self._status_at = self.now()
        return self._status

    def _kv(self, key: str) -> dict | None:
        row = self.db.q1("SELECT value FROM kv WHERE key=?", (key,))
        try:
            v = json.loads(row["value"]) if row else None
        except ValueError:
            return None
        return v if isinstance(v, dict) else None

    def _kv_set(self, key: str, value: dict | None) -> None:
        if value is None:
            self.db.ex("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                       (key, json.dumps(value), self.db.now()))

    # ── the update ─────────────────────────────────────────────────────────
    def decorate(self, info: s.UpdateInfo) -> s.UpdateInfo:
        """The update check's answer, plus what ops adds: whether Update now is on, how a running update stands, how the last one ended."""
        applying = self._kv(KEY_STATE)
        result = self._kv(KEY_RESULT)
        last = None
        if result and (at := parse(result.get("at"))) and self.now() - at < RESULT_SHOWN:
            last = s.UpdateResult(version=result["version"], ok=bool(result["ok"]), error=result.get("error"), at=at)
        return info.model_copy(update={
            "ops_available": self.available, "ops_docker": self.available and self.docker,
            "applying": s.UpdateApplying(version=applying["version"], state=applying["state"], step=applying["step"], started_at=parse(applying["started_at"])) if applying else None,  # type: ignore[arg-type]
            "last_result": last})

    async def apply_update(self, version: str) -> None:
        """Start an update from the web app (the owner's action). Returns at once; the update runs in the background."""
        if not self.client:
            raise Conflict("ops_off", "The ops container isn't set up on this install.")
        await self.refresh_status()
        if not self.docker:
            raise Conflict("updates_off", "Updating from the web app is off. Turn it on with ./l0w ops enable-updates, or run ./l0w update on the server.")
        info = self.updates.info()
        if not info.available or info.latest != version:
            raise Conflict("not_offered", f"{version} isn't the version the update check offers" + (f" ({info.latest})." if info.available else " (there is no update available)."))
        if self._kv(KEY_STATE):
            raise Conflict("update_running", "An update is already in progress.")
        if draining(self.db):
            raise Conflict("draining", "Another update is waiting for the assistant already (./l0w update?). Cancel it first.")
        self._kv_set(KEY_RESULT, None)
        self._state(version, "waiting", "Waiting for the assistant to finish what it's doing", started=self.db.now())
        set_drain(self.db, True, minutes=30, by="web")
        self._start(self._run_update(version, self.now()))

    def _start(self, coro) -> None:
        self._task = asyncio.get_running_loop().create_task(coro)

    def _state(self, version: str, state: str, step: str, *, started: str | None = None) -> None:
        """Remember where the update stands (it survives the restart). `updating_at` is when the asking of ops began: the updater's result is only
        looked for from then on, and the wait for it is counted from then."""
        cur = self._kv(KEY_STATE) or {}
        updating_at = self.db.now() if state == "updating" else cur.get("updating_at")
        self._kv_set(KEY_STATE, {"version": version, "state": state, "step": step, "started_at": started or cur.get("started_at") or self.db.now(), "updating_at": updating_at})
        self.live.emit("update")

    def _beat(self) -> None:
        set_drain(self.db, True, minutes=30, by="web")  # every call is a heartbeat; ./l0w update does the same

    async def _run_update(self, version: str, started: datetime) -> None:
        try:
            await self._wait_idle(version, started)
            self._state(version, "updating", "Downloading the new version")
            since = self.now()
            try:
                await self.client.call("POST", "/v0/update", {"version": version}, timeout=60)  # type: ignore[union-attr]
            except OpsError as e:
                raise _Failed(e.detail or e.code) from e
            await self._wait_started(version, since)
            self._state(version, "restarting", "Restarting the app")
            await self._watch(version, since)
        except _Failed as e:
            self._finish(version, False, str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  (never leave the drain on)
            log.exception("update")
            self._finish(version, False, f"Unexpected error: {e.__class__.__name__}")

    async def _wait_idle(self, version: str, started: datetime) -> None:
        wait = timedelta(minutes=self.settings.get("UPDATE_IDLE_WAIT_MINUTES"))
        while True:
            waiting = blockers(self.db)
            if not waiting:
                return
            if self.now() - started >= wait:
                raise _Failed(f"The assistant wasn't idle after {wait.total_seconds() / 60:g} minutes ({waiting[0]}). Nothing was changed; try again when it's quiet.")
            self._beat()
            self._state(version, "waiting", f"Waiting for the assistant: {waiting[0]}")
            await asyncio.sleep(self.poll)

    async def _wait_started(self, version: str, since: datetime) -> None:
        """Ops pulls the images and starts the updater in the background: wait until it says the updater is running (or it failed).
        The updater's own result file counts too: ops itself may be replaced by the update before it is asked again."""
        give_up = self.now() + timedelta(minutes=20)
        while True:
            self._beat()
            r = self._read_result()
            if r and r.get("tag") == version and (parse(r.get("started_at")) or since) >= since - timedelta(seconds=5):
                return
            try:
                st = await self.client.call("GET", "/v0/status", timeout=15)  # type: ignore[union-attr]
            except OpsError:
                st = {}
            prog = st.get("update_progress") or {}
            if prog.get("version") == version:
                if prog.get("phase") == "failed":
                    raise _Failed(str(prog.get("error") or "ops couldn't start the update"))
                if prog.get("phase") == "updater_started":
                    return
            if self.now() >= give_up:
                raise _Failed("Ops didn't start the update in 20 minutes.")
            await asyncio.sleep(self.poll)

    def _read_result(self) -> dict | None:
        if not self.result_file:
            return None
        try:
            v = json.loads(self.result_file.read_text())
        except (OSError, ValueError):
            return None
        return v if isinstance(v, dict) else None

    async def _watch(self, version: str, since: datetime) -> None:
        """Wait for the updater's result. Control is usually restarted in the middle of this; the new process picks the wait up at startup."""
        give_up = since + UPDATE_GIVE_UP
        while True:
            self._beat()
            r = self._read_result()
            finished = parse(r.get("finished_at")) if r and r.get("tag") == version else None
            if r and finished and finished >= since - timedelta(seconds=5):
                if r.get("ok"):
                    return self._finish(version, True)
                raise _Failed(_tail(str(r.get("tail") or "")) or "The updater failed.")
            if __version__ == version and not (r and r.get("tag") == version and not r.get("finished_at")):
                return self._finish(version, True)  # this is the new release, and no updater is still working
            if self.now() >= give_up:
                raise _Failed("The update didn't finish within 45 minutes. Check the server (./l0w ps), and run ./l0w update if it stopped half way.")
            await asyncio.sleep(self.poll)

    def _finish(self, version: str, ok: bool, error: str | None = None) -> None:
        """Success or failure: remember it for a day, tell the owner once, and let runs start again."""
        set_drain(self.db, False)
        self._kv_set(KEY_STATE, None)
        error = error[:1000] if error else None
        self._kv_set(KEY_RESULT, {"version": version, "ok": ok, "error": error, "at": iso(self.now())})
        if ok:
            self.notifier.notify("update", f"Updated to l0wagent {version}", "The app restarted on the new version.", "/settings/system", "update", version)
        else:
            self.notifier.notify("update", f"The update to {version} failed", (error or "")[:250], "/settings/system", "update", version)
        self.live.emit("update")
        self.live.emit("status")

    async def startup(self, loop: asyncio.AbstractEventLoop) -> None:
        """After a restart: an update that was waiting goes on waiting; one that was running is watched until the updater's result is in."""
        self.loop = loop
        if not self.client:
            return
        asyncio.get_running_loop().create_task(self.refresh_status())
        self.schedule_hub()  # what a restart interrupted
        st = self._kv(KEY_STATE)
        if not st:
            return
        started = parse(st["started_at"]) or self.now()
        if st["state"] == "waiting":
            self._start(self._run_update(st["version"], started))
        else:
            since = parse(st.get("updating_at")) or started

            async def resume() -> None:
                try:
                    await self._watch(st["version"], since)
                except _Failed as e:
                    self._finish(st["version"], False, str(e))
            self._start(resume())

    def cancel_waiting(self) -> None:
        """The owner cancelled (Settings or the banner): a wait for idle stops. One that has begun can't be stopped."""
        st = self._kv(KEY_STATE)
        if not st:
            return
        if st["state"] != "waiting":
            raise Conflict("update_running", "The update has already started; it can't be cancelled now.")
        if self._task:
            self._task.cancel()
        self._kv_set(KEY_STATE, None)
        self.live.emit("update")

    async def shutdown(self) -> None:
        tasks = [t for t in (self._task, *self._hub_tasks) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def housekeeping(self, now: datetime) -> None:
        if self.client:
            await self.refresh_status(force=False)

    # ── extensions from a git URL ──────────────────────────────────────────
    def _need(self) -> OpsClient:
        if not self.client:
            raise Conflict("ops_off", "The ops container isn't set up on this install.")
        return self.client

    @staticmethod
    def _refuse(e: OpsError) -> Exception:
        """Ops' refusals are the owner's to read: 4xx become a 400/409 with ops' own words."""
        if e.status == 0 or e.status >= 500:
            return Conflict("ops_unreachable" if e.status == 0 else "ops_failed", e.detail or "The ops container couldn't do that.")
        return BadRequest(e.code, e.detail or e.code) if e.status in (400, 403, 422) else Conflict(e.code, e.detail or e.code)

    async def resolve(self, req: s.ExtensionResolveRequest) -> s.ExtensionResolved:
        try:
            r = await self._need().call("POST", "/v0/extensions/resolve", {"git": req.git.strip(), "ref": req.ref.strip() or None}, timeout=60)
        except OpsError as e:
            raise self._refuse(e) from e
        return s.ExtensionResolved(commit=str(r.get("commit") or ""))

    async def install(self, req: s.ExtensionInstallRequest) -> str:
        """Install an extension the owner picked (source `owner`: ops records it in extensions/.ops.toml). -> its name; it stays off until the owner turns it on."""
        try:
            await self._need().call("POST", "/v0/extensions/install", {"name": req.name, "git": req.git.strip(), "commit": req.commit.strip().lower(),
                                                                       "path": (req.path or "").strip() or None, "source": "owner"}, timeout=300)
        except OpsError as e:
            raise self._refuse(e) from e
        await asyncio.to_thread(self._rescan, req.name, False)
        return req.name

    async def remove(self, name: str) -> s.ExtensionRemoved:
        rec = self.extensions.ops_record(name) if self.extensions else None
        if rec is None:
            raise Conflict("not_ours", "That extension wasn't installed from the web app (or by a hub), so it can only be removed on the server.")
        if rec.get("source") == "hub":
            raise Conflict("hub_extension", "A hub service installed this extension. Switch it off under Settings → Hub (the hub would install it again otherwise).")
        out = await self._remove(name)
        return s.ExtensionRemoved(name=name, still_in_extensions_toml=bool(out.get("still_in_extensions_toml")))

    async def _remove(self, name: str) -> dict:
        """Unload, then have ops delete the folder, then rescan (the row stays, so the extension's data and settings survive)."""
        if self.extensions and name in self.extensions.loaded:
            await asyncio.to_thread(self.extensions.enable, name, False)
        try:
            out = await self._need().call("POST", "/v0/extensions/remove", {"name": name}, timeout=60)
        except OpsError as e:
            raise self._refuse(e) from e
        await asyncio.to_thread(self._rescan, name, False)
        return out

    def _rescan(self, name: str, enable_new: bool) -> None:
        """Read the extensions folder again; a loaded extension reloads its new code; a first install of a hub's extension is turned on."""
        ext = self.extensions
        if ext is None:
            return
        was_loaded = name in ext.loaded
        ext.scan()
        if was_loaded and name in ext.found:
            ext._load(name)
        elif enable_new and name in ext.found and not was_loaded:
            try:
                ext.enable(name, True)
            except (Conflict, NotFound) as e:  # recorded in the extension's own error; the hub row says so too
                log.info("extension %s didn't start: %s", name, getattr(e, "detail", e))
        self.live.emit("extension", {"ext": name, "type": "changed", "data": {}})

    # ── a hub's extensions ─────────────────────────────────────────────────
    def schedule_hub(self) -> None:
        """Bring the installed hub extensions in line with the service rows. Runs in the background (a git fetch can take a while);
        asks that arrive while one runs make it go round once more. Safe from request threads."""
        if not self.client:
            return
        loop = self.loop
        if loop is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._spawn_hub()
        else:
            loop.call_soon_threadsafe(self._spawn_hub)

    def _spawn_hub(self) -> None:
        self._hub_pending = True
        t = asyncio.get_running_loop().create_task(self._hub_runner())
        self._hub_tasks.add(t)
        t.add_done_callback(self._hub_tasks.discard)

    async def idle(self) -> None:
        """Wait for the background work (tests)."""
        while self._hub_tasks:
            await asyncio.gather(*list(self._hub_tasks), return_exceptions=True)

    async def _hub_runner(self) -> None:
        if self._hub_lock.locked():
            return  # the running pass sees `_hub_pending` and goes round again
        async with self._hub_lock:
            while self._hub_pending:
                self._hub_pending = False
                try:
                    await self.reconcile_hub()
                except Exception:  # noqa: BLE001
                    log.exception("hub extensions")

    def _row_set(self, address: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.ex(f"UPDATE hub_services SET {cols} WHERE address=?", (*fields.values(), address))

    async def reconcile_hub(self) -> None:
        """Make the extensions installed for the hub's services match the service rows (see the module's docstring)."""
        client = self.client
        if client is None:
            return
        joined = self.m.joined()
        rows = [r for r in self.db.q("SELECT * FROM hub_services WHERE present=1")] if joined else []
        wants = {ext_name(r["address"]): r for r in rows if r["extension_json"]}
        have_ext = [r for r in rows if r["extension_json"]]
        st = await self.refresh_status()
        if st is None:
            for r in have_ext:
                if r["extension_enabled"] and r["ext_state"] in ("off", "installing"):
                    self._row_set(r["address"], ext_state="error", ext_error="The ops container isn't answering; it will be tried again.")
            if have_ext:
                self.live.emit("membership")
            return
        installed = {e["name"]: e for e in st.get("extensions") or [] if e.get("source") == "hub"}
        for name, r in wants.items():
            await self._reconcile_one(joined, r, name, installed.get(name))
        for name in installed:  # installed for a service the hub no longer lists (or we left the hub)
            if name not in wants:
                await self._drop_hub_ext(None, name, "the hub no longer lists it" if joined else "this instance left its hub")
        for r in rows:
            if not r["extension_json"] and (r["ext_state"] != "off" or r["ext_commit"]):
                self._row_set(r["address"], ext_state="off", ext_commit=None, ext_error=None)
        self.live.emit("membership")

    async def _reconcile_one(self, joined: dict | None, r: dict, name: str, cur: dict | None) -> None:
        address, ext = r["address"], json.loads(r["extension_json"])
        if not EXT_NAME.match(name):
            return self._row_set(address, ext_state="error", ext_error=f"“{name}” can't be an extension name here (2 to 31 lowercase letters, digits and dashes).")
        if not r["extension_enabled"]:
            if cur:
                await self._drop_hub_ext(r, name, "you switched it off")
            elif r["ext_state"] != "off" or r["ext_commit"]:
                self._row_set(address, ext_state="off", ext_commit=None, ext_error=None)
            return
        same = bool(cur) and cur.get("git") == ext["git"] and cur.get("commit") == ext["commit"] and _norm_path(cur.get("path")) == _norm_path(ext["path"])
        if same:
            if r["ext_state"] != "installed" or r["ext_commit"] != ext["commit"] or r["ext_error"]:
                self._row_set(address, ext_state="installed", ext_commit=ext["commit"], ext_error=None)
            return
        if cur is None:
            return await self._install_hub_ext(joined, r, name, ext, None)
        # A newer (or different) commit than the one installed.
        asked = bool(joined and joined.get("ask_before_extension_updates"))
        if asked and r["ext_approved"] != ext["commit"]:
            self._row_set(address, ext_state="waiting", ext_commit=cur.get("commit"), ext_error=None)
            if r["ext_asked"] != ext["commit"]:
                self._ask_update(r, name, cur.get("commit"), ext["commit"])
            return
        await self._install_hub_ext(joined, r, name, ext, cur)

    async def _install_hub_ext(self, joined: dict | None, r: dict, name: str, ext: dict, cur: dict | None) -> None:
        address = r["address"]
        if not joined or not r["doc_json"]:
            return self._row_set(address, ext_state="error", ext_error="The service's signed document isn't on file yet; it will be tried again after the next check with the hub.")
        self._row_set(address, ext_state="installing", ext_error=None)
        self.live.emit("membership")
        try:
            await self._need().call("POST", "/v0/extensions/install", {"name": name, "git": ext["git"], "commit": ext["commit"], "path": ext["path"],
                                                                       "source": "hub", "service_doc": json.loads(r["doc_json"]), "hub_key": joined["hub_key"]}, timeout=300)
        except OpsError as e:
            self._row_set(address, ext_state="error" if cur is None else "installed", ext_commit=cur.get("commit") if cur else None,
                          ext_error=(e.detail or e.code)[:500])
            self.m._log("extension", f"{name}: install failed ({e.code}) {e.detail}"[:900])
            return
        await asyncio.to_thread(self._rescan, name, cur is None)
        self._row_set(address, ext_state="installed", ext_commit=ext["commit"], ext_error=None)
        if cur is None:
            self.m._log("extension", f"{name}: installed at {_short(ext['commit'])} for {address}")
        else:
            self.m._log("extension", f"{name}: updated {_short(cur.get('commit'))} → {_short(ext['commit'])} for {address}")
            self.notifier.notify("hub", f"{r['title']}: extension updated", f"{name} now runs the hub's commit {_short(ext['commit'])} (was {_short(cur.get('commit'))}).",
                                 "/settings/hub", "hub_extension", name)

    async def _drop_hub_ext(self, r: dict | None, name: str, why: str) -> None:
        try:
            await self._remove(name)
        except (Conflict, BadRequest) as e:  # gone already, or ops can't: say so, and go on
            self.m._log("extension", f"{name}: couldn't remove it ({getattr(e, 'detail', e)})"[:900])
            if r:
                self._row_set(r["address"], ext_state="error", ext_error=str(getattr(e, "detail", e))[:500])
            return
        self.m._log("extension", f"{name}: removed ({why})")
        if r:
            self._row_set(r["address"], ext_state="off", ext_commit=None, ext_error=None)

    def _ask_update(self, r: dict, name: str, old: str | None, new: str) -> None:
        """The owner asked to be asked first: one Inbox card for this commit (the permission mechanism; only control can file this action)."""
        if self.conv is None:
            return
        self.conv.file_permission(
            None, None, f"Update the extension “{name}” from {r['title']}?",
            f"Your hub published a new version of the extension that goes with {r['title']}: {_short(old)} → {_short(new)}. It runs inside your control app, so it's yours to allow. "
            "Until you do, the old version stays.",
            [{"kind": "extension_update", "address": r["address"], "name": name, "from_commit": old or "", "to_commit": new}],
            trusted=True, title=f"{r['title']}: extension update waiting", tell_agent=False)
        self._row_set(r["address"], ext_asked=new)

    def apply_update_approval(self, payload: dict) -> dict:
        """The owner allowed the card (conv._apply, a request thread): remember the approval and let the background pass install it."""
        address = str(payload.get("address") or "")
        r = self.db.q1("SELECT * FROM hub_services WHERE address=? AND present=1", (address,))
        if not r or not r["extension_enabled"]:
            raise RuntimeError("that service or its extension isn't on any more")
        self._row_set(address, ext_approved=str(payload.get("to_commit") or ""))
        self.schedule_hub()
        return {"scheduled": True}

    def approve_now(self, address: str) -> None:
        """The owner pressed Update on a service whose newest commit is waiting (no card needed)."""
        r = self.db.q1("SELECT * FROM hub_services WHERE address=? AND present=1", (address,))
        if not r or not r["extension_json"]:
            return
        self._row_set(address, ext_approved=json.loads(r["extension_json"])["commit"])
        self.schedule_hub()


class _Failed(Exception):
    pass


def _tail(text: str, n: int = 600) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text

