# l0wagent: orientation for coding agents

A long-running personal AI assistant on the owner's own server. **Start with [docs/plans/development.md](docs/plans/development.md)**: it says which milestone is current and what "done" means. Don't build ahead of the current milestone; ideas for later go in that file's "Later" list.

Current milestone: **M0 (setup and contract)**.

## Sources of truth
- **UI and styling: the mockup** (`design/mockup/`; readable sources in `design/mockup/src/`). It outranks `docs/plans/ui.md`. Port screens from its markup and tokens; where ui.md disagrees, update ui.md. [design/mockup/REVIEW.md](design/mockup/REVIEW.md) lists every known difference and how it was settled.
- **API: `control/openapi.json`**, generated from the pydantic models in `control/l0w_control/api/schemas.py`. Never edit the JSON or `web/src/api/schema.d.ts` by hand.
- **Everything else: `docs/plans/`.**

## Layout
```
control/   FastAPI app (Python 3.12, SQLite, no ORM). api/ = schemas + routes; backends/base.py = the
           interface routes call; mock/ = the in-memory mock backend (M0); db/migrations/ = versioned SQL
web/       React + TypeScript + Vite. api/ = generated types, client, queries, live stream; app/ = shell;
           screens/; components/; styles/tokens.css = the mockup's theme tokens
design/mockup/  the mockup bundle as exported, its extracted sources, and REVIEW.md
```
Created in their milestones: `agent/`, `egress/`, `agent-web/`, `chat-runtime/`, `cli/`, `dev/`.

## Commands
```
cd control && uv sync                                  # once
cd control && uv run uvicorn l0w_control.app:app --reload --port 8000   # mock API on :8000
cd web && pnpm install && pnpm dev                     # UI on :5173, proxies /api to :8000
docker compose -f compose.dev.yaml up --build          # both, in containers

cd control && uv run pytest && uv run ruff check .
cd web && pnpm typecheck && pnpm test
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

## Commits
Authored by the owner only. **No AI co-author or "generated with" lines.** Commit only when the owner asks.
