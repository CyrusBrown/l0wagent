# l0wagent: orientation for coding agents

A long-running personal AI assistant on the owner's own server. **Start with [docs/plans/development.md](docs/plans/development.md)**: it says which milestone is current and what "done" means. Don't build ahead of the current milestone; ideas for later go in that file's "Later" list.

Current work: **the hub, stages H5 (friends and messages) and H6 (hub services, the ops container)**, per [docs/plans/hub-development.md](docs/plans/hub-development.md) (owner, 2026-11-17: start the hub; 2026-11-22: continue the plan). H5 and H6 (hub services, the ops container) are built and waiting on the owner's checks; the ops updater hasn't run against real Docker yet. H1–H4 are built and waiting on the owner's checks and spec review. The first real hub services, the **board and shared spaces** (owner, 2026-11-22), are built in `~/Dev/l0server/L0wHub/l0wservices` (specs `protocol/board.md`, `protocol/spaces.md`; TS library `protocol/ts/`; SDK 1.2); they pass end to end (`l0wservices/e2e`) and wait on a deploy. M9 (more opencode providers and the model registry) is not started. M1–M8 are waiting on the owner's review; the desktop app is on the Later list.

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
           harness news (news.py); chat.py (the 1:1 chat: loop, model clients, cards, hand-off);
           tools.py (the chat's and the connector's tools, written once); chat_settings.py (settings the chat
           reads and proposes); guide/ (the user guide the chat's `help` tool reads: keep it current);
           catalog.py (opencode model catalogs and prices); notify.py (web push, ntfy);
           integrations.py (brokered MCP servers); connector.py + api/connector_routes.py (/mcp, OAuth);
           updates.py (update check, draining); hostctl.py (host-side commands for ./l0w);
           membership.py (hub membership: join, self-description, self-check, rotate, leave; keys stay in the gateway);
           hubservices.py (a hub's services: rows follow the capabilities, connect/hello/welcome, role grants held in control, calls and the call log);
           breaker.py (the spend breaker for peer activity: spans, limits, the owner's card);
           friends.py + presence.py (friends, peer threads and messages, wake requests; what we tell friends we're up to; the agent's
           friend routes are in api/agent.py and its wording in prompts/peers.md);
           extensions.py + sdk.py (the owner's extensions: loader, isolation, the SDK v1 contract);
           db/migrations/ = versioned SQL
agent/     the agent image: runner/ (runner.py, fake_claude.py, fake_opencode.py, opencode-plugin.js, selftest.py),
           bin/ (assistant CLI, hook), seed/. Two runtimes: Claude Code and opencode (for opencode Go)
egress/    the egress proxy (from the old assistant)
web/       React + TypeScript + Vite. api/ = generated types, client, queries, live stream; app/ = shell;
           screens/; components/; styles/tokens.css = the mockup's theme tokens; e2e/ = Playwright
dev/       isolation-test.sh and notes on the fake runtime
design/mockup/  the mockup bundle as exported, its extracted sources, and REVIEW.md
cli/       l0w.py, the host CLI (`./l0w`: install, update, ops parity); stdlib only
deploy/    control.Dockerfile (control + built web app); compose.yaml at the root is the release stack
chat-runtime/  the stateless sidecar for Claude-subscription chats (Claude Code, no tools of its own, its own network)
extensions/    the owner's extensions (git-ignored) + AGENTS.md and examples/plantlog; the guide is docs/EXTENDING.md
protocol/      the hub protocol spec (l0w/0.1): documents, schemas, test vectors, py/ = l0wproto (reference library), conformance/
gateway/       the instance's key holder: makes and signs with the instance keys, never hands a private key out (hubnet + its own gateway-out)
tunnel/        the spec tunnel client: dials out to the hub, hands requests to control / the files app by Host (hubnet + outside)
hub/           the generic hub core on Cloudflare (Workers, Durable Objects, D1); its own CLAUDE.md; depends only on protocol/
dev/hub/       the fake hub (Python), for development and tests; dev/check-boundaries.py keeps instance and hub apart
```

## Commands
```
cd control && uv sync                                  # once
cd control && uv run python scripts/dev_stack.py --port 8000   # control + runner (fake claude) + egress, no Docker
cd control && uv run python scripts/dev_stack.py --port 8000 --hub   # plus the fake hub (:8770), gateway and tunnel; prints an invite
cd control && L0W_BACKEND=mock uv run uvicorn l0w_control.app:app --reload --port 8000   # the in-memory mock API
cd web && pnpm install && pnpm dev                     # UI on :5173, proxies /api to :8000
dev/ui-demo.sh                                         # the UI on fake data (mock API + Vite), nothing else needed
docker compose -f compose.dev.yaml up --build          # the whole stack in containers (fake claude by default)
./l0w --help                                           # the host CLI (install/update use compose.yaml + prebuilt images)

cd control && uv run pytest && uv run ruff check .     # includes the restart integration tests (no Docker)
cd web && pnpm typecheck && pnpm test && pnpm e2e      # e2e starts its own stack on :8100/:5174
dev/isolation-test.sh                                  # isolation probes against the running compose stack
cd gateway && uv run pytest -q; cd tunnel && uv run pytest -q   # the instance's hub-side services
cd dev/hub && uv run pytest -q                          # the fake hub + the conformance suite against it
cd hub && pnpm test && ./scripts/conformance.sh         # the Cloudflare hub core, conformance against wrangler dev
python3 dev/check-boundaries.py                        # instance and hub/ depend only on protocol/
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
- `l0w_control/sdk.py` is a public contract (SDK 1.x): add to it, don't break it; any change goes into
  `docs/EXTENDING.md` and a test in `control/tests/test_extensions.py`. Core code never imports an extension.
- The fake runtime's scenarios (`agent/runner/fake_claude.py`) are how the agent's side is tested
  without a login: extend them when you add agent-facing behavior. `fake_opencode.py` wraps them in
  opencode's event format, so they cover both runtimes.
- Runtimes: a run's provider picks it (`budgets.RUNTIME`, `Budgets.runtime_spec`). Pin the CLIs'
  versions in `agent/Dockerfile`; the adapters read their headless event streams.

## Security invariants (from docs/plans/server-platform.md; binding)
- The agent container is on the sandbox network only: no Docker socket, no host mounts beyond explicit read-only grants, no NET_ADMIN/NET_RAW. Nothing it serves is reachable from outside the sandbox.
- Everything inside the agent container is untrusted; enforcement lives in control and egress.
- Sandbox clients may only call `/api/agent/*` with the agent token. Agent endpoints never let the agent approve permissions, answer its own questions, finish owner tasks, write secrets, change settings, start runs, or change budgets.
- Secret values are write-only for people and redacted from everything control stores or renders.
- Model-provider calls from the sandbox need an egress lease. The usage gate fails closed.
- Prompts are control-owned; archive old text before edits; harness news announces agent-facing changes.
- Control writes into the agent's storage only for owner uploads into `_inbox/` and extensions' managed folders.
- Files are served safely: symlink-safe containment; agent HTML only in a sandboxed frame on a separate origin, SVG only as an image; agent markdown sanitized.
- Offline-queued actions never include approvals, secrets, settings or budget changes.
- `/api/peer/*` is the instance-to-instance inbox, reachable only through the hub tunnel (everyone else gets 404);
  `/api/hub/*` stays reserved for the hub (later).
- Browsers send a host's cookies to every port on it, including the files origin. So control refuses
  cookie-authenticated writes without the `X-L0W-Client` header (the web client sends it). Agent HTML
  loads only from the files origin (its port, or its own hostname on a hub), with the sandbox CSP on
  every response; images, media, SVG (as `<img>`) and PDFs (pdf.js) render inertly in the UI.
- Requests through the hub tunnel (`L0W_TUNNEL_SUBNET`) are their own caller class: never
  `/api/agent/*` or `/api/chat-runtime/*`, never "local", client address only from the tunnel's header.
- Device tokens are scoped (`full`, `cli`, `read`; `auth.scope_allows`); browser sessions are full.
- Hub services: role grants a service signs for the instance stay in control (callers name a role, control attaches the grant); everything a service sends (results, events, titles) is untrusted content, labelled so for the agent; a service never authorizes anything for the assistant. `service-*` envelopes are accepted only from the addresses the hub's capabilities list as services and the owner has on.
- The `ops` container (H6): `ops` has no Docker access; only control calls it (its token, on the internal `opsnet`), and it reaches the internet (`ops-out`) only for git over https and the release check. It installs extensions only at a pinned commit, from the owner's own action, `extensions.toml` or a hub's signed service list, and updates only to real releases. `ops-docker` (opt-in at install) is the only service with the Docker socket: it offers just status, pulling this project's images and starting the updater, and builds every Docker request itself. Neither is on the sandbox network, `hubnet` or the tunnel's network; control still has no Docker socket.
- Only the gateway holds the instance's private keys (signing, sealing); the recovery key is shown to
  the owner once and never stored. The gateway is on `hubnet` (control and the tunnel) and its own `gateway-out` (the internet, for
  peer delivery; no other container joins it; targets vetted like egress), never the sandbox network; the tunnel reaches control only by its `control-hub` alias on `hubnet`, so its
  requests always fall in `L0W_TUNNEL_SUBNET`. Joining a hub needs TOTP or a passkey.

## Commits
Authored by the owner only. **No AI co-author or "generated with" lines.** Commit only when the owner asks.
