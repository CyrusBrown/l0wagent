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

**Status (2026-11-15): built.** The done-when flow is the browser test `web/e2e/m1.spec.ts`; `control/tests/test_runs_integration.py` also covers the agent computer restarting mid-run (resumed with `--resume`), stopping, and the fail-closed gate; `dev/isolation-test.sh` passes on Docker Desktop. Decisions along the way:
- **Deferred (owner, 2026-11-14): the breached-password check.** Passwords are argon2id with a 12-character minimum.
- **The runner owns sessions.** It appends each session's output to a log on the agent's home volume and keeps running when control disconnects. Control stores each log line and the new offset in one transaction. While control is down, nothing supervises a session, but the egress lease (240 s, renewed every 30 s) runs out, so the agent loses model access. An agent-container restart marks the session `lost`; control resumes it (up to `RUN_MAX_RESUMES`).
- **Usage before budgets:** `Status.budget` is absent until M4, and Home and the status chip show the provider's own meter (weekly and 5-hour %). The gate refuses runs when usage is unknown (`USAGE_UNKNOWN_POLICY=refuse`) or at the reserve (80% weekly, 70% 5-hour). `compose.dev.yaml` sets `allow`, because the fake runtime has no login.
- **Fake runtime:** `agent/runner/fake_claude.py` replaces the old fake runner. The real runner drives it, so tests exercise the runner too.
- **Idempotency keys:** the table and contract exist, but M1 has no offline-queued creates (starting a run is online only). The first user is M2's messages.
- **UI additions not in the mockup**, designed in its style: Settings → Account and System, the M1 steps of the setup wizard, and "Wake now" on Runs (see `design/mockup/REVIEW.md`).

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

**Status (2026-11-15): built, without a logged-in Claude account.** The fake runtime plays the agent: it reads the thread, claims it, replies, files a question, and picks up your answer through the real `assistant-hook` after a tool call. The done-when flow is the browser test `web/e2e/m2.spec.ts` and `test_reply_now_claims_asks_and_hears_the_answer`. Decisions along the way:
- **Still unverified with real Claude Code:** that the PostToolUse hook fires in a real session (same settings file as the old assistant), and how a real model uses the new commands. One logged-in session covers both.
- **Reply now has no cost yet.** It runs on `REPLY_TIER` (default sonnet) for up to `REPLY_MAX_MINUTES` (15), and is available whenever no run is active and the usage gate is open. The reply budget and promote-to-run arrive in M4. Promote cards can be decided, but nothing files them yet.
- **Holding a run open:** if a session messaged you or left an ask open, the run waits `HOLD_MINUTES` (5) for your response and resumes the same conversation with it, up to `HOLD_MAX_ROUNDS` (3). A message to a thread while a run is active reaches the agent after its next tool call; otherwise it waits for the next run's brief.
- **Approval scopes:** `always` keeps the action. `once` undoes it when the run that asked ends (egress rule removed, setting restored, secret deleted). `project` is offered when the ask is in a project's thread and for now behaves like `always`; it is recorded so project-scoped enforcement can come later.
- **Secrets (basic):** encrypted at rest, write-only, all in the agent's environment (scope "agent only"), readable by the agent with `assistant secret get` (counted). Values are redacted from everything control stores, and setting a secret scrubs it from stored history. The owner adds them on Tools & Secrets or through a secure-input card (a permission whose actions are secrets).
- **Uploads came forward from M3:** the composer attaches files, and control writes them into `/work/_inbox/<project|general>/` through a volume subpath mount. `/work` is otherwise read-only to control, and writes never follow symlinks. The files browser and shelf stay in M3.
- **Typed links:** `@project` and `#thread` in the composer insert `l0wagent://` links, and your messages render as sanitized Markdown so the links work. `/file` links wait for M3.
- **Suggestions are carried out by control** when accepted (rename, move, archive, merge, split, stale), with "moved here" system messages.
- **UI additions not in the mockup** (see `design/mockup/REVIEW.md`): a thread actions menu (rename, move, pin, archive), a Decline button on secure-input cards, and the project view's Asks and Activity tabs filled with real data.

## M3: Files
**Goal:** see and get what the assistant makes.
- **Storage split**: `/workspace`, `/work` (with `_inbox/`), `/cache` volumes.
- **Shelf**: live and snapshot items, kept versions, `assistant shelf add`.
- **Uploads** into `_inbox/` (control's only write path there).
- **Files browser**: `/workspace` and `/work`, previews, downloads, git history and diffs, "add to shelf".
- **Previews**: markdown, code, images, PDF, CSV, audio, video, 3D (STL/STEP/3MF), HTML and notebooks, with agent-made HTML on a **separate origin** in a sandboxed frame. Symlink-safe path resolution.
- The **assistant's web page** panel (port 8787).

**Done when:** the assistant shelves a script's output, it updates in the project's Files tab when the script reruns, and an uploaded file reaches the assistant.

**Status (2026-11-15): built, without a logged-in Claude account.** The fake runtime reads an uploaded CSV, shelves a script's output live with `--keep 3`, and reruns the script; the done-when flow is the browser test `web/e2e/m3.spec.ts`, with `control/tests/test_files.py` covering the browser, git history, the files origin and the shelf. Decisions along the way:
- **Shelf copies live in control's data dir** (`shelf/<item>/<n>`), never in the agent's storage. Control watches live items and stores a version when one changes (if the item keeps versions); a missing file marks the item missing, not removed.
- **Reading the agent's storage is symlink-safe:** every path is opened one component at a time with `O_NOFOLLOW`, and special files are refused. Git history comes from dulwich (pure Python), so control never runs `git` on the agent's repos.
- **The files origin** is a second port of control (`L0W_FILES_PORT`, 8001 in compose). The UI sends HTML, SVG, PDFs, images and media there through short-lived signed links scoped to one file, folder or shelf item; no cookies are used, every response carries a `sandbox` CSP (except PDFs, which browsers won't render sandboxed), and frames add their own `sandbox`.
- **3D previews:** STL and 3MF with three.js; STEP converted in the browser by occt-import-js (WebAssembly), loaded only when needed.
- **Attachments in thread history:** a reply run's brief and `assistant messages` list each message's attachments, so an upload still reaches the agent when an earlier run already delivered the message.
- **Not yet checked in Docker** in this pass: `compose.dev.yaml` now publishes the files origin; run the stack and `dev/isolation-test.sh` before calling M3 reviewed.

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

**Status (2026-11-16): built, without a logged-in Claude account.** The browser test `web/e2e/m4.spec.ts` picks a level, sets a standing instruction and wakes the assistant: the fake runtime checks its budget, asks the reviewer, registers a long job and plans its next wakeup, which shows on the Calendar. `control/tests/test_budgets.py` covers points, levels, Boost, the ledger, wakeup validation, the fallback, the sweep, routines, jobs, the reviewer's gate and tier changes, and **a simulated two weeks** in which a planning agent stays within its allowance; `test_m4_integration.py` covers a wakeup with the reviewer, a promoted reply and a run stopped at its budget. Decisions along the way:
- **Points** (owner): 1 pt = 0.1% of a Claude subscription's week, $0.10 on an API key. Levels Dormant 0 / Frugal 50 / Steady 150 / Busy 400 / Max 850 pts a week; each level also sets allowed hours, wakeups per day, gap, points per wakeup, tiers, sweep and fallback (`budgets.py`). The owner can override any of them; choosing a level clears the overrides; a Boost uses its level's limits as they are.
- **Measuring the assistant's use:** each model message's tokens are priced at API rates as they arrive (live `used`, so the monitor stops a run at its budget), then replaced by the runtime's own `total_cost_usd`. On a subscription, dollars become points through a calibration learned from how far the weekly meter moved during runs (starts at 2 pts/$). The owner's own use is the weekly meter minus the assistant's and chat use. **Unverified with a real account**: the calibration's starting value and how fast it settles.
- **Buckets:** Reply-now runs spend the reply budget (at most `REPLY_RUN_MAX_PTS`, 6, each); an approved promote moves the rest of the run onto the allowance; sweep runs spend only room below the sweep ceiling, on top of the allowance. Owner-started runs (Wake now) default to `MANUAL_RUN_PTS` (20) and aren't capped by the allowance; neither are wakeups the owner adds on the Calendar.
- **Fallback:** if nothing is planned in the next 48 hours, control plans one at 03:00 (owner's time, moved into the allowed hours) for 10% of the allowance (Steady: 15 pts) and tells the agent; any wakeup the agent plans replaces it.
- **Missed wakeups:** one run at a time; a wakeup that can't start within `WAKEUP_LATE_MINUTES` (30) is marked missed and the agent hears about it.
- **Routines** are owner-created or agent-proposed (a permission card whose action is the routine), plus the built-in weekly review 6 hours before the weekly reset. They keep their own schedule (not bound by allowed hours) but spend the allowance. Dormant skips them.
- **The reviewer** runs through the runner's new `/consult` (a second, read-only `claude -p`; no hook, secrets or agent token) and is ported from the old turbo reviewer: Monitor by default, watching every 10 minutes only when something new happened, at most 25% of a wakeup's budget, one conversation per day. Gate mode holds back `IDLE` (up to 3 rounds) and `claim done`. `LEVEL:` lines switch the worker's tier by interrupting and resuming the session. **Unverified with a real model**: how often it interjects and whether the tier switch's resume is smooth.
- **Jobs:** `assistant job start|progress|done`; logs must live under `/work` or `/workspace` and are tailed through the symlink-safe reader; the runner's `/procs` tells control when a job's process is gone (marked lost, the agent is told). "Don't interrupt" is recorded for M5's updates.
- **Prompts:** core, brief, reply, reviewer and the weekly review rewritten (placeholders archived in `prompts/archive/`); harness news announces the new commands. The workspace seed now has the full layout and a real manual.
- **Not done:** the planner model and the adaptive reserve (both "Later"); the utility role (stored, not used yet); the "now" sentence fallback; detecting detached processes as jobs.
- **UI additions not in the mockup** (see `design/mockup/REVIEW.md` #32–36).

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
- The **breached-password check** at login and setup (k-anonymity range API), deferred from M1.

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
