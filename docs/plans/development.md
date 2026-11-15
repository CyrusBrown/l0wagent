# Development plan

The order of work, what each milestone delivers, and how we know it's done. Part of the [overhaul plan](README.md).

**Rule: the assistant and its UI come first.** Everything hub-related (friends, board, email, shared spaces, published services, streams) and the other optional extras wait until the owner says to start them. Early milestones leave **seams** for them (auth modes, capability flags, UI slots), not features.

## Tracks
- **Backend** (control, runner, agent image, egress) and **UI** (the React app, starting from the mockup) move together. Every milestone includes the screens for what it builds, so each one ends with something usable end to end.
- **The old assistant** keeps running on the home server, untouched, until migration (M7).

## Repo layout

```
l0wagent/
  control/       FastAPI app (Python 3.12, SQLite, no ORM) + tests
  web/           React + TypeScript + Vite app (from the mockup) + tests
  agent/         agent image: runner, `assistant` CLI, hook, workspace seed
  egress/        egress proxy (carried over from the old repo, adapted)
  agent-web/     LAN forwarder for the agent's port (carried over)
  chat-runtime/  stateless sidecar for CLI-only chat providers (M6)
  cli/           `l0w` host CLI (successor to `scripts/ops`)
  dev/           fake runner, fixtures, mock data
  design/mockup/ the mockup as exported (kept for reference)
  docs/          plans, operations, architecture
  compose.yaml  compose.dev.yaml  .github/workflows/
  CLAUDE.md      orientation for coding agents (invariants, conventions, commit rules)
```

Code is reused from the old repo where it fits: egress, the agent-web forwarder, the runner's process handling, vault redaction, the usage reader, and parts of the `assistant` CLI and `ops`.

## Definition of done (every milestone)
- **Tests**: unit tests for control (pytest) and the web app (Vitest), plus a fake-runner integration test of the milestone's main flow. From M1, a browser test (Playwright) of that flow.
- **Isolation checks still pass**: the agent can't reach the internet except through egress, can't reach the LAN, the control UI or the ops/owner API, and can't reach model providers without a lease.
- **The OpenAPI spec is updated**, and the web app's types are regenerated from it.
- **Docs**: the plan files and `CLAUDE.md` reflect what was built, including decisions made along the way.
- **It runs** locally with `compose.dev.yaml` (on the Mac, with Docker).

---

## M0: Setup and contract
**Goal:** a repo where both tracks can start, and one contract they share.
- Repo skeleton (layout above), `CLAUDE.md` for coding agents (the invariants from [server-platform.md](server-platform.md#security-invariants); conventions; commits authored by the owner only, no AI co-author lines), CI (lint + tests).
- **Import the mockup** into `design/mockup/` and start `web/` from it.
- **Mockup review**: list where the mockup and [ui.md](ui.md) differ, and settle each (update the plan or the mockup).
- **Data model** (first schema) and **OpenAPI v0** covering M1–M3 screens: auth, meta, sync feed, live stream, projects, threads, messages, asks, claims, suggestions, Inbox, runs, files and shelf.
- **Mock server** generated from the spec, so the web app runs against realistic data before the backend exists.
- `compose.dev.yaml` with control, web dev server and the fake runner.

**Done when:** the mockup runs as the web app against the mock server, and the spec is reviewed.

## M1: Core platform
**Goal:** log in, start a run, watch it live, and nothing breaks if control restarts.
- **Control skeleton**: FastAPI, SQLite with versioned migrations, settings (`config.SPECS`-style), the request guard (sandbox, owner, agent token), and the auth-mode seams (session, device token, agent token; OAuth and signed requests come later).
- **Owner account and login**: one owner, username + password (argon2id, breached-password check, rate limits, lockout), optional TOTP, sessions and device tokens, new-device alerts (Inbox card).
- **API foundations**: `/api/meta`, the live event stream (fetch-readable with a token), the sync feed, idempotency keys.
- **Agent stack**: the agent image, egress (lease, rules, log), agent-web. The runner writes events to an append-only log, and control **re-attaches after a restart**; the agent resumes with `--resume` after an agent restart.
- **Runtime adapter interface + Claude Code adapter**; the Claude subscription usage reader; the fail-closed usage gate.
- **`assistant` CLI basics**: note, status, run describe, inbox, wait, notify, news.
- **Isolation self-test** (runnable from the UI).
- **UI**: login, Home (status, current run), a basic runs list and live run timeline, Settings → account and sessions.

**Done when:** you log in, start a manual run, watch its timeline live, restart control mid-run, and the run carries on.

## M2: Conversations
**Goal:** talk to the assistant the way the UI plan describes.
- **Projects, threads, messages**: General and per-project threads, created by owner or assistant; archive; unread counts.
- **Asks**: questions (four kinds + default + note), permissions with actions (egress rule, secret, setting), tasks. Mid-run delivery through the hook, `assistant wait`, holding a run open for replies.
- **Claims** and **suggestions** (thread and project level, accept/dismiss, "Accept all").
- **Reply now** (a simple version; the reply budget arrives in M4) and the **queued** indicator.
- **Typed links** (`l0wagent://…` and in-app), **search** and the command palette.
- **Secrets, basic**: write-only vault, redaction, env injection to the agent only (the full Tools & Secrets page is M6).
- **UI**: sidebar, thread view with cards and composer (paste, attachments), project view, projects list, Inbox with History.

**Done when:** you message the assistant, press Reply now, and it replies, claims a task and files a question you answer in place, all live.

## M3: Files
**Goal:** see and get what the assistant makes.
- **Storage split**: `/workspace`, `/work` (with `_inbox/`), `/cache` volumes.
- **Shelf**: live and snapshot items, kept versions, `assistant shelf add`.
- **Uploads** into `_inbox/` (control's only write path there).
- **Files browser**: `/workspace` and `/work`, previews, downloads, git history and diffs, "add to shelf".
- **Previews**: markdown, code, images, PDF, CSV, audio, video, 3D (STL/STEP/3MF), HTML and notebooks, with agent-made HTML on a **separate origin** in a sandboxed frame. Symlink-safe path resolution.
- The **assistant's web page** panel (port 8787).

**Done when:** the assistant shelves a script's output, it updates in the project's Files tab when the script reruns, and an uploaded file reaches the assistant.

## M4: Budgets, wakeups and runs
**Goal:** the assistant runs itself within the budgets you set. This replaces nightly, review and turbo.
- **Providers and roles** (Claude subscription and Anthropic API; opencode waits for a key, see M8), the **usage ledger** with you-vs-assistant attribution.
- **Budgets**: allowance, reserve (80% weekly / 70% 5-hour), allowed hours, wakeup limits, allowed tiers, level presets and Boost, the **reply budget** and **promote-to-run** (approval card).
- **Wakeup planning**: `assistant wake plan|at`, `assistant budget`, control-side validation, the **fallback** wakeup (2 nights), the **sweep**.
- **Routines**: owner-created and agent-proposed, plus the built-in weekly review (on only with a Claude subscription).
- **Reviewer**: on by default in Monitor mode, with mode, tier rights, interval and budget share.
- **Stopping contract** (`IDLE:`) and the **job registry**.
- **Runs**: the timeline normalizer, the simple and detailed views, file-change tallies.
- **Prompts rewrite** (core, wakeup brief, reviewer) and the **new workspace seed**.
- **UI**: Calendar, Settings → budgets and levels / providers / roles / reviewer / standing instructions, both run views, long jobs.

**Done when:** a test instance runs unattended for a week within its budget, schedules a timed wakeup for a real event, and its runs read clearly in both views.

## M5: Installable and shareable (first friends release)
**Goal:** a friend can install it from the README and keep it updated.
- **Prebuilt images** on GHCR (stable/edge), `compose.yaml`, the `compose.override.yaml` for host resources (GPU, memory, mounts).
- **Installer** (`./l0w install`) and the **first-run wizard**.
- **Updates**: the update check, `./l0w update`, waiting for idle, the update banner. The opt-in updater sidecar is optional here.
- **`l0w` CLI** with `ops` parity, for owners and their coding agents.
- **Installable web app**: manifest, offline cache and outbox, **web push** (with Tailscale HTTPS documented), notification settings (approvals and new-device logins on by default).
- **Docs**: install, operations, troubleshooting, Docker Desktop notes (with the self-test).

**Done when:** a friend installs on a clean Linux machine without your help, gets a push notification on their phone, and later updates without losing a run.

## M6: Chat, tools and the connector
**Goal:** the 1:1 chat, Tools & Secrets, and outside chat services.
- **1:1 chat**: the in-control loop for API providers and the **chat-runtime sidecar** for Claude subscriptions; tools (read assistant data and the workspace, create threads and projects, post as the owner, wake the assistant); hand-off; cards for sensitive actions; independent chats.
- **Tools & Secrets**: secret scopes (agent-only default), **brokered MCP servers**, sandbox MCP registration, per-tool policy, the **GitHub preset**, **secure input cards**, guided setup from the chat.
- **MCP connector** at `/mcp` with OAuth 2.1 and scopes, plus the downloadable skill and the "open in app" link page. Reachable on the LAN/Tailscale until a hub exists.
- **UI**: Chats, Tools & Secrets (secrets, integrations, connected apps), Network page.

**Done when:** you plan something in a chat, hand it to the assistant as a new project thread, set up GitHub through a secure card, and use the connector from Claude Desktop.

## M7: Migrate your instance (timing is yours)
Once M5 (and ideally M6) work, move the current assistant over as planned in [migration.md](migration.md): export, fresh instance, legacy folder, archived threads, the re-setup run.

## M8: Extensions and the second runtime
- **Extension SDK v1 with UI support**: everything in [server-platform.md](server-platform.md#extensions), including what hub-service extensions will need (gateway calls, background jobs, managed folders, Inbox and thread types). Plus `extensions/AGENTS.md` and `docs/EXTENDING.md`, and one example extension.
- **opencode adapter** (Zen and other API providers) as soon as there's a key to test with. It can move earlier if a key turns up.

**Done when:** a coding agent, following only the guide, builds a small extension with a page and an agent command, and it survives an update.

## M9: Desktop app
The Tauri app per [ui.md](ui.md#desktop-tauri): connect screen, login → device token, the cached server UI, native notifications, tray, `l0wagent://` links, drag-and-drop, and CI-built unsigned installers for Mac, Windows and Linux.

**Done when:** installed from a CI build on each OS, it works offline with queued messages and opens `l0wagent://` links.

---

## Later: not started until you say
- **Hub**: core and l0w.run on Cloudflare, friends and presence, the board, email, shared spaces, published services, streams, linking. Order in [hub.md](hub.md#build-order). Most of it plugs in as first-party extensions (needs M8).
- **More providers** (Google AI Studio, OpenRouter, local models), the planner model, the adaptive reserve.
- The extension catalog, backups, the CAD visual diff, the desktop app starting a local Docker stack.

### Seams to keep while building the early milestones
So the later work doesn't need rewrites:
- Auth modes with room for **signed requests** (`/api/peer/*`, `/api/hub/*` reserved).
- `/api/meta` **capabilities**, so features can be switched on when a hub advertises them.
- Message `sender` and thread types that can grow (peer, email).
- UI **extension slots** (sidebar sections, pages, Inbox card types, thread types, previewers, settings panels) used by the core screens themselves.
- `/work/_inbox/` subfolders reserved for email and peers.

## Risks to watch
- **Claude Code's headless interface changes**: keep the adapter thin, and pin the CLI version.
- **Usage measurement**: agent-only attribution needs calibration. Start simple (per-run deltas with token counts) and refine in M4.
- **Docker Desktop isolation** may differ from Linux: the self-test must catch it before friends rely on it.
- **opencode is untested**: until M8, everything assumes Claude Code, with the adapter interface kept honest.
- **Scope creep**: anything not in the current milestone goes into this file's later list, not into the build.
