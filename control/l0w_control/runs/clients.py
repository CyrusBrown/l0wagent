"""HTTP clients for the runner (inside the agent container) and the egress proxy's admin API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..config import Env


class RunnerError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RunnerIdle(RunnerError):
    """The log stream was open but went quiet past the idle timeout: not lost contact."""


HEARTBEAT = b"\x1e"  # the runner's keep-alive line on a quiet log stream (agent/runner/runner.py)


class RunnerClient:
    # The log stream is reconnected whenever it goes quiet this long, which also
    # notices a runner that disappeared without closing the connection.
    LOG_IDLE_TIMEOUT = 30.0  # the runner sends a heartbeat every 10 s, so 30 s of silence means trouble

    def __init__(self, env: Env):
        self.url = env.runner_url
        self.headers = {"X-Runner-Token": env.runner_token}

    async def _call(self, method: str, path: str, timeout: float = 15, **kw) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.request(method, f"{self.url}{path}", headers=self.headers, **kw)
        except httpx.HTTPError as e:
            raise RunnerError(f"runner unreachable: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise RunnerError(f"runner: HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json()

    async def health(self) -> dict:
        return await self._call("GET", "/health", timeout=10)

    async def start(self, payload: dict) -> str:
        return (await self._call("POST", "/sessions", json=payload, timeout=30))["id"]

    async def session(self, sid: str) -> dict:
        return await self._call("GET", f"/sessions/{sid}")

    async def stop(self, sid: str, force: bool = False) -> dict:
        return await self._call("POST", f"/sessions/{sid}/stop", json={"force": force})

    async def commit(self, message: str, since: str | None) -> dict:
        return await self._call("POST", "/commit", json={"message": message, "since": since}, timeout=120)

    async def refresh_auth(self) -> dict:
        return await self._call("POST", "/auth/refresh", timeout=180)

    async def login_start(self) -> dict:
        return await self._call("POST", "/auth/login", timeout=60)

    async def login_code(self, code: str) -> dict:
        return await self._call("POST", "/auth/login/code", timeout=120, json={"code": code})

    async def consult(self, payload: dict) -> dict:
        """One reviewer turn (blocks until it ends)."""
        return await self._call("POST", "/consult", timeout=600, json=payload)

    async def procs(self, pids: list[int]) -> set[int]:
        return set((await self._call("GET", f"/procs?pids={','.join(map(str, pids))}"))["alive"])

    async def selftest(self, targets: dict) -> list[dict]:
        return (await self._call("POST", "/selftest", json={"targets": targets}, timeout=60))["checks"]

    async def log(self, sid: str, offset: int) -> AsyncIterator[tuple[dict, int]]:
        """Yield (event, size in bytes) for each complete line after `offset`, following
        the session until it ends. Partial trailing lines are left for the next read."""
        timeout = httpx.Timeout(connect=10, read=self.LOG_IDLE_TIMEOUT, write=10, pool=10)
        connected = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("GET", f"{self.url}/sessions/{sid}/log", params={"offset": offset}, headers=self.headers) as r:
                    if r.status_code >= 400:
                        raise RunnerError(f"runner log: HTTP {r.status_code}", r.status_code)
                    connected = True
                    yield None, 0  # in touch: the stream is open
                    buf = b""
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        while (nl := buf.find(b"\n")) >= 0:
                            line, buf = buf[:nl], buf[nl + 1 :]
                            if line == HEARTBEAT:  # the runner is there; not part of the log, not counted
                                yield None, 0
                                continue
                            size = len(line) + 1
                            try:
                                event = json.loads(line) if line.strip() else None
                            except json.JSONDecodeError:
                                event = {"type": "raw", "text": line.decode(errors="replace")[:4000]}
                            yield (event or {"type": "blank"}), size
        except httpx.ReadTimeout as e:
            if connected:  # an open stream went quiet (a runner older than heartbeats, or a slow one): reconnect quietly
                raise RunnerIdle(f"runner log: idle for {self.LOG_IDLE_TIMEOUT:g}s") from e
            raise RunnerError(f"runner log: {type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            raise RunnerError(f"runner log: {type(e).__name__}: {e}") from e


class EgressClient:
    """The model-provider lease is shared: several holders (a run, a login refresh) can need
    it at once, and it's revoked only when the last one releases it. Holders live in memory,
    so after a restart control re-acquires for each run it re-attaches to."""

    def __init__(self, env: Env):
        self.url = env.egress_url
        self.headers = {"X-Egress-Token": env.egress_token}
        self.holders: dict[str, str] = {}

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.request(method, f"{self.url}{path}", headers=self.headers, **kw)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError as e:
            raise RunnerError(f"egress: {type(e).__name__}: {e}") from e

    async def status(self) -> dict:
        return await self._call("GET", "/status")

    async def set_lease(self, active: bool, ttl: int, reason: str = "") -> dict:
        return await self._call("POST", "/lease", json={"active": active, "ttl_seconds": ttl, "reason": reason})

    async def acquire(self, holder: str, ttl: int, reason: str) -> None:
        self.holders[holder] = reason
        await self.set_lease(True, ttl, reason)

    async def renew(self, ttl: int) -> None:
        if self.holders:
            await self.set_lease(True, ttl, "; ".join(self.holders.values()))

    async def release(self, holder: str, reason: str = "") -> None:
        self.holders.pop(holder, None)
        if not self.holders:
            await self.set_lease(False, 0, reason)

    async def add_rule(self, pattern: str, action: str, note: str = "") -> int:
        return int((await self._call("POST", "/rules", json={"pattern": pattern, "action": action, "note": note}))["id"])

    async def delete_rule(self, rule_id: int) -> None:
        await self._call("DELETE", f"/rules/{rule_id}")
