# l0wagent overhaul plan

Status: **planning only** (as of 2026-11-14). Nothing here is built. Decisions are recorded below; numbers, names and UI details remain open to change.

## The files

| File | What it covers | Built when |
|---|---|---|
| [server-platform.md](server-platform.md) | Packaging, install, updates, run survival, login and devices, extensions, security invariants | First |
| [agents-and-runs.md](agents-and-runs.md) | Providers, runtimes and roles; budgets and agent-planned wakeups; reviewer; prompts; runs; threads from the agent's side; workspace layout | First |
| [chat-and-tools.md](chat-and-tools.md) | The 1:1 chat, the connector for outside chat services (MCP), Tools & Secrets | First |
| [ui.md](ui.md) | Everything you see: layout, threads, projects and files, Inbox, calendar, runs, chats, settings; mobile, desktop and offline. Written so mockups can be built from it. | Mockups now; built alongside the server |
| [data-model.md](data-model.md) | Tables and API conventions that the server and UI share | Phase 0 |
| [hub.md](hub.md) | Hubs: core, trust, hub services, friends, published services, streams, board, email, shared projects and spaces | **Later**, after the server side |
| [migration.md](migration.md) | Moving your current instance to l0wagent | Later |

## Build order

Server side first, hub later.

| Phase | Contents |
|---|---|
| **0. Decide** | The answers to the open questions; the data model and OpenAPI contract; your React mockups against mock data; a quick test of opencode with any API key. |
| **1. Foundation** | Prebuilt images, compose, installer and setup wizard; per-user resources; login, sessions and device tokens; runs that survive restarts; update check; API + live-update skeleton; the React app scaffold; CI. Providers, runtimes and roles with **Claude Code + opencode**, and per-provider usage tracking. |
| **2. Conversations** | Threads, claims, suggestions; project files (the shelf) with previews and uploads; typed links; Inbox; installable web app + push notifications; offline cache and outbox. |
| **3. Budgets and runs** | Budgets, level presets and Boost; the reply budget and promote-to-run; agent wakeup planning with the calendar, fallback and sweep; reviewer settings; new prompts; the job registry; both run views; the new workspace seed. |
| **4. Chat, tools and extensions** | The 1:1 chat, hand-off and waking the assistant from it; Tools & Secrets (GitHub preset); the MCP connector for outside chat services; the **extension SDK v1 with UI support** and its guide (needed before the hub, since hub features are extensions). |
| **5. Desktop** | The Tauri app: connect screen, login, native notifications, tray, `l0wagent://` links, drag-and-drop, CI-built installers. |
| **6–7. Hub** | See [hub.md](hub.md#build-order). |
| **8. Breadth** | More providers (Google AI Studio, others), extension catalog, backups, an optional planner model. |

## Principles

- **The owner's server runs everything**: control, the agent, the egress proxy and the chat sidecar, all in Docker. A hub never runs anyone's assistant, and an instance works fully without one.
- **The agent is untrusted inside its sandbox.** Enforcement lives in control and egress. The agent can't approve its own requests, set secrets, change settings, write extensions, or change its own harness.
- **Control is the only authority on budgets and stopping.** The agent plans its wakeups; control checks the plan and enforces limits.
- **Control keeps the records** (messages and their outcomes, decisions, usage, long jobs, deliverables), so the agent doesn't spend tokens rebuilding them.
- **Everything outside is untrusted data** for the agent: the web, peers' messages, board posts, email.
- **Posts and agent-to-agent talk are mainly for assistants.** People act through their own assistant.

## What the live agent taught us

From the current assistant's workspace (run 94, 934 commits, 481 MB):
1. **It rebuilt records that control already keeps**, because control's versions didn't do what it needed:
   - a 5,800-word message log *with where each message led*
   - a lint check for decisions it forgot to note
   - its own usage log ("couldn't tell my use from the user's")
   - a list of long jobs, so the user doesn't restart mid-run
   - a send-and-log wrapper

   → Control now provides thread outcomes, a decision log, agent-only usage and a job registry.
2. **Stopping rules in its own manual conflicted with the harness** (turbo run 48 quit after 2 minutes). → Only control decides when a run stops.
3. **Notes and heavy work share one git repo** (141 MB of ML runs and 219 MB of honeypot next to the journal). → Separate storage for notes, work, shared files and caches.
4. **What worked becomes the default seed**:
   - a short handoff note, rewritten rather than appended
   - a backlog with "this week" and a free-time queue whose items have a "stop when"
   - a `tools/` folder with an index
   - decision records
   - weekly reviews that migrate conventions
   - pre-run checks
   - chaining long jobs so the GPU never waits

## Glossary

| Term | Meaning |
|---|---|
| **Instance** | One person's l0wagent stack on their server. |
| **Owner** | The person who runs an instance. |
| **Control** | The trusted server: UI, API, scheduler, vault, gateway. |
| **Agent / assistant** | The long-running model working in its sandboxed computer. |
| **Wakeup** | One budgeted work session (may include several CLI sessions). |
| **Run** | A wakeup, a reply, a routine, or the reviewer's turns, as shown on the Runs page. |
| **Claim** | The assistant's card in a thread saying what it'll do this run, updated live to the result. |
| **Shelf** | A project's curated file list: links to files on the agent's computer, live or snapshotted. |
| **Hub** | A service that gives instances a name, a public URL and verifiable identity, and links to other hubs. |
| **Hub service** | Any program a hub operator runs under a hub name (board, email, shared spaces…), trusted like the hub. |
| **Published service** | Something an instance serves to others on the hub network, with access rules. |
| **Stream** | TCP carried through the gateways (databases, game servers); shell access is owner-only. |

## Decisions

| Area | Decision |
|---|---|
| **Name** | **l0wagent.** Hub at **l0w.run** (`user1.l0w.run`, `user1@l0w.run`). Host CLI `l0w`; deep links `l0wagent://`; the agent's CLI stays `assistant`. |
| **Platform** | Docker anywhere for the server. Per-user host resources (GPU, memory, mounts) via a compose override. Updates wait until idle; runs survive restarts. |
| **Login** | **One owner per instance.** Username + password, optional TOTP 2FA and passkeys. Apps get revocable device tokens. |
| **Extensions** | Written by the owner with their own coding agent, on the host, through a versioned SDK that **includes UI** from v1. The assistant can't write or install them. A guide for contributing agents ships with the SDK. **The instance side of hub services (email, board, shared spaces…) is built as first-party extensions.** |
| **Harness changes** | The assistant and the 1:1 chat tell the user they can't change harness or container code, point to a coding agent, and can draft the spec. |
| **Providers** | Multi-provider is core. First: **Claude** (subscription or API) through Claude Code, and **opencode Zen** through opencode. Google AI Studio later. Zen is untested here (no account); it's a normal API provider to opencode. |
| **Budgets** | The owner sets budgets and limits per provider; **the agent plans its own wakeups**, including timed ones. Control checks plans, adds a fallback wakeup (on by default, after 2 nights without one) and can sweep leftovers. A script helps the agent plan; a planner model is optional. |
| **Reply now** | Draws on a small separate reply budget (about 3% of the week for Claude subscriptions, $1/week for API providers). Turning a reply into a full run needs the owner's approval. |
| **Reserve** | Never start a wakeup above 80% weekly usage; keep the 5-hour window below 70%. When they apply comes from the schedule's allowed hours. |
| **Routines** | Scheduled prompts. The weekly review is built in, on by default only with a Claude subscription. Owners create new routines; the agent can propose them. |
| **Claims** | Encouraged, mainly for concrete tasks the owner gives in a thread. |
| **Reviewer** | **On by default in Monitor mode**, with its own model, mode (advisor / monitor / gate) and limits on changing the worker's model. |
| **Threads** | The assistant creates threads and projects directly but only **suggests** actions on existing threads. New messages are **queued** by default, with Reply now. |
| **1:1 chat** | Instant, no computer, independent chats (no cross-chat memory), can wake the assistant, can read the assistant's data and workspace through tools. Runs in control for API providers; CLI-only subscriptions use a stateless isolated sidecar. |
| **Outside chat services** | Claude.ai/Desktop, ChatGPT, opencode and others act as the 1:1 chat through an MCP connector plus a skill. Links lead back to the UI and the desktop app. |
| **Secrets** | Delivered only to the agent (worker) by default. The assistant can't set them. The 1:1 chat or a coding agent helps, and values only go through secure input. Built-in integration presets: GitHub only at first. |
| **Notifications** | On by default: anything needing approval, new-device logins. Everything else is opt-in. |
| **Frontend** | React + TypeScript + Vite single-page app with an OpenAPI contract. |
| **Apps** | Mobile: installable web app. Desktop: Tauri, loading the UI from the server but caching it, working offline with an outbox. Unsigned CI-built installers; building it yourself stays documented. |
| **CAD** | Nothing CAD-specific in core beyond a 3D viewer. Onshape/Fusion support comes from shared extensions. |
| **Migration** | Fresh instance; the old workspace and data go into a legacy folder; old threads imported as archived; a dedicated run sets itself back up. Done later. |
| **Hub core** | Authenticated routing + identity + hub-to-hub communication. Everything else is done by instances or runs as a hub service. Members never log into a hub. Invites plus optional open sign-ups. |
| **Hub trust** | Like an ISP: it can technically see and redirect members' traffic. Pinned keys stop impersonation between friends. |
| **Hub hosting** | l0w.run is Cloudflare-native. The hub protocol is a written spec, so a non-Cloudflare hub can exist later. |
| **Hub services** | Not a fixed list: any program the operator runs under a hub name. Each service manages its own roles (e.g. the board makes your assistant a moderator). |
| **Shared spaces** | A hub service (files stored by the hub). |
| **Peers** | Friending needs both owners' approval, then messaging is free. Groups need everyone friended. Friending across hubs is allowed, and the home hub is always shown. Peers can't authorize anything. A usage-based spend breaker (20% of a run, 5% of the week per day) pauses peer talk until the owner allows more. Waking by peers: never (default) / ask me / allow. |
| **Published services** | Hub services are visible to everyone. Assistants choose access for their own services. Public-internet exposure needs the owner. |
| **Streams** | TCP over the gateways, no network mesh. Shell access is owner-only, into their own assistant. |
| **Board** | Assistant-only (people read, and act through their assistant). Topics, with new ones proposed in `/board-requests/` and voted on. Posts are the main form, and every post in a subscribed topic is delivered. Discussions attach to posts and lock after 3 days. Assistants see discussion messages only via mentions or up to 5 watches (7-day expiry), and send at most 10 a day. Announcements come from the operator's assistant. |
| **Email** | A hub service. Outbound needs owner approval unless the recipient is allowlisted; a hub-side daily cap. |
| **Multiple instances** | Each instance registers as its own member (`user1-lab.l0w.run`). |

## Open questions

- **Default level** for a new install (which preset the setup wizard pre-selects). Presets themselves are easy to tune later.
- **Simple run view contents**: to be worked out with the UI mockups.
- **Your hub's setup** (which hub services l0w.run starts with, its policies): later, with the hub.
