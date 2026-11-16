# l0wagent: orientation for coding agents

A long-running personal AI assistant on the owner's own server. **Start with [docs/plans/development.md](docs/plans/development.md)**: it says which milestone is current and what "done" means. Don't build ahead of the current milestone; ideas for later go in that file's "Later" list.

Current milestone: **M4 (budgets, wakeups and runs)**, built on top of M1–M3; all four are waiting on the owner's review. M5 (installable and shareable) is next.

## Sources of truth
- **UI and styling: the mockup** (`design/mockup/`; readable sources in `design/mockup/src/`). It outranks `docs/plans/ui.md`. Port screens from its markup and tokens; where ui.md disagrees, update ui.md. [design/mockup/REVIEW.md](design/mockup/REVIEW.md) lists every known difference and how it was settled.
- **API: `control/openapi.json`**, generated from the pydantic models in `control/l0w_control/api/schemas.py`. Never edit the JSON or `web/src/api/schema.d.ts` by hand.
- **Everything else: `docs/plans/`.**

## Layout
```
control/   FastAPI app (Python 3.12, SQLite, no ORM). api/ = schemas + owner routes + agent.py (/api/agent/*);
           backends/base.py = the interface routes call, sqlite.py = the real backend, mock/ = in-memory
           fixtures for UI work; auth.py; guard.py (request guard); live.py (live stream + sync feed);
           conv.py (projects, threads, messages, asks, claims, suggestions, delivery to the agent);
           budgets.py (points, levels, ledger, wakeup validation, roles, reviewer config); scheduler.py
           (due wakeups, fallback, sweep, routine occurrences); routines.py; reviewer.py; jobs.py;
           vault.py (write-only secrets, redaction); runs/ = run manager, runtime adapters,
           runner/egress clients, usage reader + gate; prompts/ = control-owned prompts and
           harness news (news.py); db/migrations/ = versioned SQL
agent/     the agent image: runner/ (runner.py, fake_claude.py, selftest.py), bin/ (assistant CLI, hook), seed/
egress/    the egress proxy (from the old assistant)      agent-web/  the LAN forwarder for port 8787
web/       React + TypeScript + Vite. api/ = generated types, client, queries, live stream; app/ = shell;
           screens/; components/; styles/tokens.css = the mockup's theme tokens; e2e/ = Playwright
dev/       isolation-test.sh and notes on the fake runtime
design/mockup/  the mockup bundle as exported, its extracted sources, and REVIEW.md
```
Created in their milestones: `chat-runtime/`, `cli/`.

## Commands
```
cd control && uv sync                                  # once
cd control && uv run python scripts/dev_stack.py --port 8000   # control + runner (fake claude) + egress, no Docker
cd control && L0W_BACKEND=mock uv run uvicorn l0w_control.app:app --reload --port 8000   # the in-memory mock API
cd web && pnpm install && pnpm dev                     # UI on :5173, proxies /api to :8000
docker compose -f compose.dev.yaml up --build          # the whole stack in containers (fake claude by default)

cd control && uv run pytest && uv run ruff check .     # includes the restart integration tests (no Docker)
cd web && pnpm typecheck && pnpm test && pnpm e2e      # e2e starts its own stack on :8100/:5174
dev/isolation-test.sh                                  # isolation probes against the running compose stack
```

## Changing the API
1. Edit `control/l0w_control/api/schemas.py` and `api/routes.py`; add the method to `backends/base.py` and implement it in `mock/backend.py` (a test fails if the mock misses one).
2. `cd control && uv run python scripts/export_openapi.py` (CI fails if `openapi.json` is stale).
3. `cd web && pnpm gen:api`.
4. If a screen test needs the new data: `cd control && uv run python scripts/dump_mock_responses.py`.

## Conventions
- **Contract-first**: every create takes an `idempotency_key`; live updates are NDJSON on `GET /api/events`, read with `fetch`; `GET /api/sync?since=` feeds the offline cache.
- Schema changes are new files in `control/l0w_control/db/migrations/NNNN_name.sql`; never edit an applied migration.
- Mock data follows the plan's workspace seed and `_inbox/` folders; it lives in `control/l0w_control/mock/fixtures.py`.
- Web styling: plain CSS on the mockup's tokens (`web/src/styles/`), not Tailwind; it matches the mockup exactly. Light and dark themes come from the same tokens.
- TypeScript is pinned to 5.x: openapi-typescript needs the JS compiler API that TypeScript 7 dropped.
- Runs: the runner appends each session to a log; control stores each line and the new offset in one
  transaction, so re-attaching after a restart never loses or repeats an event. Never make a session
  depend on control's connection. Test restart behavior with `control/tests/stack.py`.
- Control-owned prompts live in `control/l0w_control/prompts/` (placeholders until M4). Any change the
  agent will notice (a new `assistant` command, a changed behavior) gets a new entry in `prompts/news.py`.
- The owner decides; the agent proposes. Agent routes (`api/agent.py`) may create threads, projects,
  messages, asks, claims and suggestions and withdraw its own asks, never answer, decide, resolve or
  edit existing threads. Approved permission actions are applied by control (`conv._apply`).
- Everything stored from the agent or the owner goes through `vault.redact`.
- The fake runtime's scenarios (`agent/runner/fake_claude.py`) are how the agent's side is tested
  without a login: extend them when you add agent-facing behavior.

## Security invariants (from docs/plans/server-platform.md; binding)
- The agent container is on the sandbox network only: no Docker socket, no host mounts beyond explicit read-only grants, no NET_ADMIN/NET_RAW. Port 8787 reaches the LAN only through `agent-web`.
- Everything inside the agent container is untrusted; enforcement lives in control and egress.
- Sandbox clients may only call `/api/agent/*` with the agent token. Agent endpoints never let the agent approve permissions, answer its own questions, finish owner tasks, write secrets, change settings, start runs, or change budgets.
- Secret values are write-only for people and redacted from everything control stores or renders.
- Model-provider calls from the sandbox need an egress lease. The usage gate fails closed.
- Prompts are control-owned; archive old text before edits; harness news announces agent-facing changes.
- Control writes into the agent's storage only for owner uploads into `_inbox/` and extensions' managed folders.
- Files are served safely: symlink-safe containment; agent HTML/SVG only in a sandboxed frame on a separate origin; agent markdown sanitized.
- Offline-queued actions never include approvals, secrets, settings or budget changes.
- `/api/peer/*` and `/api/hub/*` are reserved for the hub (later).
- Browsers send a host's cookies to every port on it, including the agent's page (8787) and the
  files origin. So agent-web strips `Cookie`, `Authorization` and `Set-Cookie`, and control refuses
  cookie-authenticated writes without the `X-L0W-Client` header (the web client sends it).

## Commits
Authored by the owner only. **No AI co-author or "generated with" lines.** Commit only when the owner asks.
