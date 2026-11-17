#!/usr/bin/env python3
"""Real-account smoke suite for a running l0wagent stack. See README.md.

    L0W_URL=http://localhost:8000 L0W_TOKEN=<device token> python dev/smoke/smoke.py [--only a,b] [--budget 15]
    cd control && uv run python ../dev/smoke/smoke.py --dry-run     # plumbing check on the fake stack

Everything goes through the owner API with a device token (`./l0w login` makes one). Scenarios leave
the owner's instructions and reviewer settings as they found them, and cancel the wakeups they planned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx is needed: run this with control's environment (cd control && uv run python ../dev/smoke/smoke.py ...)")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
ENDED = ("done", "failed", "stopped", "lost", "error", "cancelled")


class Skip(Exception):
    pass


class Api:
    def __init__(self, url: str, token: str):
        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=60, headers={"Authorization": f"Bearer {token}", "X-L0W-Client": "smoke"})

    def call(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, json=body if body is not None else {}, **kw)

    def put(self, path, body):
        return self.call("PUT", path, json=body)

    def delete(self, path):
        return self.call("DELETE", path)


class Ctx:
    """What a scenario gets: the API, its output folder, and a checklist."""

    def __init__(self, api: Api, out: Path, dry: bool, budget: float, wait: float):
        self.api, self.out, self.dry, self.budget, self.wait = api, out, dry, budget, wait
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "", fake_ok: bool = True) -> None:
        # On the fake stack only the plumbing is promised; checks the fake can't produce are noted, not failed.
        self.checks.append((name, bool(ok) or (self.dry and not fake_ok), detail if ok else (detail + (" (dry run: not expected on the fake)" if self.dry and not fake_ok else ""))))

    def until(self, pred, what: str, timeout: float | None = None, every: float = 2.0):
        end = time.time() + (timeout or self.wait)
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(every)
        raise TimeoutError(f"timed out waiting for {what}")

    def save(self, name: str, data) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
        (self.out / name).write_text(text)

    def run_to_end(self, run_id: int, label: str) -> dict:
        def ended():
            r = self.api.get(f"/api/runs/{run_id}")["run"]
            return r if r["status"] in ENDED else None
        try:
            run = self.until(ended, f"run #{run_id} to end")
        finally:
            self.record_run(run_id, label)
        return run

    def record_run(self, run_id: int, label: str) -> list[dict]:
        try:
            items = self.api.get(f"/api/runs/{run_id}/timeline", params={"view": "detailed"})
            self.save(f"{label}-run{run_id}-timeline.json", items)
            self.save(f"{label}-run{run_id}-timeline.txt", "\n".join(f"{i['at']}  [{i['kind']}] {i['text']}" for i in items))
            self.save(f"{label}-run{run_id}-detail.json", self.api.get(f"/api/runs/{run_id}"))
            return items
        except Exception as e:  # recording must never hide the real failure
            self.save(f"{label}-run{run_id}-timeline.error", str(e))
            return []


def texts(items: list[dict]) -> list[str]:
    return [i["text"] for i in items]


class Restore:
    """Puts the owner's instructions and reviewer config back, whatever the scenario did."""

    def __init__(self, api: Api):
        self.api = api
        self.instructions = api.get("/api/instructions")
        self.reviewer = api.get("/api/reviewer")

    def instructions_to(self, text: str) -> None:
        self.api.put("/api/instructions", {"text": text})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.api.put("/api/instructions", {"text": self.instructions.get("text", "")})
            self.api.put("/api/reviewer", self.reviewer)
        except Exception as e:
            print(f"  WARNING: couldn't restore instructions/reviewer: {e}", file=sys.stderr)


def cancel_new_wakeups(ctx: Ctx, started_at: str) -> None:
    try:
        for w in planned_wakeups(ctx, started_at):
            ctx.api.delete(f"/api/wakeups/{w['id']}")
    except Exception as e:
        print(f"  WARNING: couldn't cancel planned wakeups: {e}", file=sys.stderr)


def planned_wakeups(ctx: Ctx, since: str) -> list[dict]:
    cal = ctx.api.get("/api/calendar", params={"start": since})
    return [w for w in cal["wakeups"] if w.get("created_by") == "assistant" and w["status"] == "planned"]


# ── scenarios ──────────────────────────────────────────────────────────────
def wakeup_plans_next(ctx: Ctx) -> None:
    now = ctx.api.get("/api/meta")  # also proves the token works
    ctx.save("meta.json", now)
    since = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    with Restore(ctx.api) as r:
        r.instructions_to("fake:m4 SMOKE TEST. Ignore your usual work. Check your budget, ask the reviewer one question "
                          "(`assistant reviewer ask`), then plan your next wakeup with `assistant wake at` for tomorrow 03:00 "
                          "with reason 'Smoke test follow-up', then end with IDLE:.")
        try:
            run_id = ctx.api.post("/api/runs", {"budget": ctx.budget, "reason": "smoke: wakeup"})["id"]
            run = ctx.run_to_end(run_id, "a-wakeup")
            items = ctx.api.get(f"/api/runs/{run_id}/timeline", params={"view": "detailed"})
            ctx.check("run ended cleanly (done, idle)", run["status"] == "done" and run["end_reason"] == "idle", f"{run['status']}/{run['end_reason']}")
            ctx.check("run spent budget", run["used"] > 0, f"used {run['used']}")
            ctx.check("reviewer was consulted", any(i["kind"] == "reviewer" or i["text"].startswith("Reviewer") for i in items),
                  "no reviewer item on the timeline")
            ctx.check("a wakeup was planned", any(t.startswith("Planned a wakeup") for t in texts(items)), "no 'Planned a wakeup' on the timeline")
            planned = planned_wakeups(ctx, since)
            ctx.check("the plan is on the calendar", bool(planned), "no assistant-planned wakeup found")
        finally:
            cancel_new_wakeups(ctx, since)


def reply_now_question(ctx: Ctx) -> None:
    t = ctx.api.post("/api/threads", {"title": "Smoke: reply now", "idempotency_key": f"smoke-{int(time.time())}",
                                      "first_message": "SMOKE TEST. Before answering, ask me exactly one multiple-choice question with `assistant ask` "
                                                       "(options: Saturday, Sunday), wait for my answer, then reply with the day I chose. Keep it short."})
    tid = t["id"]
    run_id = ctx.api.post(f"/api/threads/{tid}/reply-now")["id"]

    def items():
        return ctx.api.get(f"/api/threads/{tid}/items")["items"]
    try:
        q = ctx.until(lambda: next((i["question"] for i in items() if i["type"] == "question"), None), "the assistant's question")
        ctx.check("a question was filed", True, q["text"] if "text" in q else str(q)[:120])
        ctx.api.post(f"/api/questions/{q['id']}/answer", {"value": (q.get("options") or ["Sunday"])[-1]})
        run = ctx.run_to_end(run_id, "b-reply-now")
        ctx.check("run ended cleanly", run["status"] == "done", f"{run['status']}/{run['end_reason']}")
        bodies = [i["message"]["body"] for i in items() if i["type"] == "message"]
        ctx.check("the assistant replied after the answer", len(bodies) >= 2, f"{len(bodies)} messages")
        ctx.save("b-thread.json", items())
    except TimeoutError as e:
        ctx.check("a question was filed", False, str(e))
        try:
            ctx.api.post(f"/api/runs/{run_id}/stop")
        finally:
            ctx.record_run(run_id, "b-reply-now")
            ctx.save("b-thread.json", items())


def gate_mode_end(ctx: Ctx) -> None:
    with Restore(ctx.api) as r:
        r.instructions_to("fake:wrapup SMOKE TEST. Do one tiny thing (`assistant note 'smoke'`), then end with a one-line wrap-up and IDLE:.")
        ctx.api.put("/api/reviewer", {**r.reviewer, "mode": "gate"})
        run_id = ctx.api.post("/api/runs", {"budget": ctx.budget, "reason": "smoke: gate"})["id"]
        run = ctx.run_to_end(run_id, "c-gate")
        items = ctx.api.get(f"/api/runs/{run_id}/timeline", params={"view": "detailed"})
        ctx.check("run was in gate mode", run["reviewer_mode"] == "gate", run["reviewer_mode"])
        ctx.check("reviewer was consulted before the end", any(i["kind"] == "reviewer" for i in items), "no reviewer item", fake_ok=False)
        ctx.check("run ended cleanly (done)", run["status"] == "done", f"{run['status']}/{run['end_reason']}")


def opencode_run(ctx: Ctx) -> None:
    provs = [p for p in ctx.api.get("/api/providers") if p["runtime"] == "opencode" and p["enabled"]]
    if not provs:
        raise Skip("no enabled opencode provider (add opencode Go in Settings → Providers)")
    p = provs[0]
    models = p.get("models") or []
    if not models:
        raise Skip(f"{p['id']} has no enabled models")
    tier = f"{models[0]['id']}/medium"
    with Restore(ctx.api) as r:
        r.instructions_to("fake:wrapup SMOKE TEST. Do one tiny thing (`assistant note 'smoke'`), then end with a one-line wrap-up and IDLE:.")
        run_id = ctx.api.post("/api/runs", {"budget": ctx.budget, "tier": tier, "reason": "smoke: opencode"})["id"]
        run = ctx.run_to_end(run_id, "d-opencode")
    items = ctx.api.get(f"/api/runs/{run_id}/timeline", params={"view": "detailed"})
    ctx.check("run used the opencode provider", run["provider"] == p["id"], str(run["provider"]))
    ctx.check("run ended cleanly", run["status"] == "done", f"{run['status']}/{run['end_reason']}")
    ctx.check("the model was recorded", any(t.startswith("Model ") for t in texts(items)), "no 'Model …' item")


def sidecar_chat(ctx: Ctx) -> None:
    chats_provider = next((p for p in ctx.api.get("/api/providers") if p["kind"] == "claude-subscription" and p["enabled"]), None)
    if not chats_provider:
        raise Skip("no enabled Claude subscription provider")
    try:
        chat = ctx.api.post("/api/chats", {"message": "SMOKE TEST. Reply with one short sentence saying hello.", "title": "Smoke chat",
                                           "provider": chats_provider["id"], "idempotency_key": f"smoke-chat-{int(time.time())}"})
    except RuntimeError as e:
        raise Skip(f"can't start a chat: {e}") from e
    cid = chat["id"]
    if not chat.get("available", True):
        raise Skip("the chat sidecar isn't available (is chat-runtime up and the agent logged in?)")

    def msgs():
        return ctx.api.get(f"/api/chats/{cid}/messages")
    try:
        ctx.until(lambda: any(m["role"] == "assistant" for m in msgs()) and not ctx.api.get(f"/api/chats/{cid}")["busy"], "the chat reply")
    finally:
        ctx.save("e-chat-messages.json", msgs())
    reply = next((m for m in msgs() if m["role"] == "assistant"), None)
    ctx.check("a reply was stored", bool(reply and reply["body"].strip()), (reply or {}).get("body", "")[:80])
    ctx.check("the reply names its model", bool(reply and reply.get("model")), str((reply or {}).get("model")))


SCENARIOS = {"a": wakeup_plans_next, "b": reply_now_question, "c": gate_mode_end, "d": opencode_run, "e": sidecar_chat}
NAMES = {"a": "wakeup plans the next one", "b": "Reply now with a question", "c": "gate-mode end", "d": "opencode run", "e": "sidecar chat turn"}


def run_all(api: Api, out: Path, dry: bool, only: list[str], budget: float, wait: float) -> int:
    results = []
    for key in only:
        ctx = Ctx(api, out, dry, budget, wait)
        print(f"[{key}] {NAMES[key]} ...", flush=True)
        status = "pass"
        try:
            SCENARIOS[key](ctx)
            if not all(ok for _, ok, _ in ctx.checks):
                status = "FAIL"
        except Skip as e:
            status = f"skipped ({e})"
        except Exception as e:
            status = f"ERROR ({type(e).__name__}: {e})"
            ctx.save(f"{key}-error.txt", traceback.format_exc())
        for name, ok, detail in ctx.checks:
            print(f"    {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
        print(f"    -> {status}")
        results.append({"scenario": key, "name": NAMES[key], "status": status, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in ctx.checks]})
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(results, indent=2))
    print(f"Recorded in {out}")
    return 0 if all(r["status"] == "pass" or r["status"].startswith("skipped") for r in results) else 1


def fake_stack():
    """--dry-run: the fake runner + egress + control from control/tests/stack.py, no Docker, no account."""
    sys.path.insert(0, str(REPO / "control"))
    from tests.stack import PASSWORD, Stack

    tmp = Path(tempfile.mkdtemp(prefix="l0w-smoke-"))
    stack = Stack(tmp, steps=4, delay=0.2, runner_env={"FAKE_JOB_S": "2"}).up()
    stack.login()
    r = stack.client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD, "want": "device_token", "device_label": "smoke"})
    return stack, r.json()["device_token"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("L0W_URL", "http://localhost:8000"))
    p.add_argument("--token", default=os.environ.get("L0W_TOKEN"))
    p.add_argument("--only", default="a,b,c,d,e", help="comma-separated scenario letters")
    p.add_argument("--budget", type=float, default=15, help="points each run may spend")
    p.add_argument("--wait", type=float, default=600, help="seconds to wait for each run or reply")
    p.add_argument("--dry-run", action="store_true", help="start the fake stack and run against it (needs control's env)")
    p.add_argument("--out", help="output folder (default dev/smoke/out/<date>)")
    a = p.parse_args()
    only = [k.strip() for k in a.only.split(",") if k.strip()]
    if bad := [k for k in only if k not in SCENARIOS]:
        p.error(f"unknown scenario(s): {bad}")
    out = Path(a.out) if a.out else HERE / "out" / date.today().isoformat() / ("dry-run" if a.dry_run else "real")
    stack = None
    try:
        if a.dry_run:
            stack, token = fake_stack()
            api = Api(stack.url, token)
            wait = min(a.wait, 120)
        else:
            if not a.token:
                p.error("set L0W_TOKEN (a device token: ./l0w login, then see ~/.config/l0w/config.json) or pass --token")
            api, wait = Api(a.url, a.token), a.wait
        return run_all(api, out, a.dry_run, only, a.budget, wait)
    finally:
        if stack:
            stack.down()


if __name__ == "__main__":
    sys.exit(main())
