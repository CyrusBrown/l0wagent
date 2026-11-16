# Agents and runs

Providers, budgets and wakeups, the reviewer, prompts, runs, threads from the agent's side, and the workspace layout. Part of the [overhaul plan](README.md).

## Carried over from today's assistant
These keep working as they do now (reworked only where this plan says so):
- **Asks**, all non-blocking: the agent files them and moves on.
  - **Questions**: yes/no, single choice, multiple choice or free text, always with a default and an optional note from the owner.
  - **Permissions**: yes/no, with **actions applied on approval**: an egress rule, a secret the owner types in, a setting. Later, more action kinds (e.g. publishing a service).
  - **Tasks**: things only the owner can do, marked done or won't-do with a note.

  The agent can withdraw but never resolve them. Approvals carry a **scope**: *always*; *once* (control undoes the actions when the run that asked ends); *project* (offered for asks in a project's thread; recorded, and treated like *always* until project-scoped rules exist). Responses are pushed mid-run (after its next tool call) or delivered in the next wakeup brief.
- **Mid-run delivery**: new messages, decisions, answers, finished tasks and new secrets reach the agent after its next tool call (the hook, or the runtime's equivalent).
- **`assistant wait N`**: pause (no tokens spent) until the owner responds, up to a limit.
- **`assistant notify`**: a note to its future self, delivered once at the next inbox check, e.g. as the last line of a background job.
- **Holding a run open for replies**: if the agent messaged the owner or asked something during a run, the run waits a few minutes before ending, and resumes the same conversation if the owner responds. Under the new budgets this counts against the run's own budget, and the owner's reply goes through the reply budget if the run already ended.
- **Run notes** (`assistant note`), now alongside `status` and `run describe`.
- **Harness news**: dated entries shown once in a wakeup brief, always readable with `assistant news`.
- **Its own machine**: passwordless sudo, cron, `system/provision.sh` (replayed after image rebuilds), `system/boot.sh` (every start), `system/pre-run.sh` (output becomes the pre-run report), and its web port (8787) for pages it serves to the owner.
- **The egress proxy**: every request logged, owner-managed block/allow rules, the LAN blocked by default, and model-provider access only under a lease.
- **The usage gate fails closed**: if usage can't be read, nothing starts (configurable).
- **Safety-net commit** of `/workspace` at the end of every run.
- **The `ops` CLI** becomes the **`l0w` CLI** on the host: everything the UI does (status, waiting, read and send, answer, approve and deny, tasks, secrets, runs, start and stop), for the owner or a coding agent working with them.

## Providers, runtimes and roles

### Providers
Credentials plus a usage source, all kept in control's vault.
- **First targets**:
  - `claude-subscription`: Pro/Max login. Usage = 5-hour and weekly %, read from the usage endpoint.
  - `anthropic-api`: an API key. Usage = $ from token counts.
  - `opencode-go` (M6): an opencode Go subscription key (`OPENCODE_API_KEY`), models as `opencode-go/<model>`. Usage = $ from opencode's per-step `cost` (catalog prices from models.dev), counted against the Go plan's limits; 1 pt = $0.10 until Go exposes a usage meter.
  - `opencode-zen` (later): a Zen key, the same runtime; pay per token.
- **Later**: Google AI Studio, other OpenAI/Anthropic-compatible endpoints (OpenRouter, LiteLLM, local models), ChatGPT via Codex.
- Each friend uses their own accounts. Two *people* sharing one subscription may break the provider's terms; one person running several instances doesn't.

### Runtimes
The agent CLI that drives a session. Each provider's sanctioned client is used, which matters for subscription terms.
- **Claude Code** for the Claude providers.
- **opencode** for opencode Go (M6), then Zen and most other providers.

The **runtime adapter** covers:
- start, resume, stream normalized events, deliver mid-run messages, stop, report usage
- capability flags: hooks, resume, effort levels, subagents

**Mid-run delivery** without Claude Code's hook uses an opencode plugin hook if available. Otherwise the `assistant` CLI appends pending notifications to any command's output, and there's an `inbox` tool.

**Phase-0 test** (with any API key): opencode's headless mode, event stream, resume, plugin hooks, running without permission prompts, and how reasoning effort maps.

### Roles
Each role has a provider, model and effort, plus allowed ranges:
- **worker**: may list several tiers across providers (e.g. a cheap model for bulk reading, a strong one for code)
- **reviewer**
- **replier**: quick replies
- **chat**: the owner's 1:1 chats
- **utility**: titles, summaries, the "now" sentence fallback, digest summaries

### Usage
- **Budgets are per provider, in that provider's unit**: % of the week (Claude subscription), $ (API keys), requests/day (free tiers), unlimited (local). The agent sees, for example: *"Claude: 11 pts left this week (reserve 40%); Zen: $3.10 of $5."*
- **Agent-only usage**: token counts per session, priced per provider. For a Claude subscription, tokens are converted to % with a calibration learned while only the agent was running. The dashboard shows **you vs. the assistant**.
- **Adaptive reserve** (later): learn how much the owner typically uses per weekday and protect that automatically.
- **Credentials across containers**: check early whether two runtimes refreshing one Claude login invalidate each other. Fallback: control keeps the full login (to read usage), and runtimes get a long-lived inference token.

## Budgets and agent-planned wakeups

This replaces today's nightly, review, turbo and chat modes.

### Budget terms

| Term | Meaning |
|---|---|
| **Allowance** | The share of a provider's usage the assistant may plan with this week (e.g. 15% of a Claude subscription's week, or $5 of an API key). It comes out of usage the owner might otherwise use, so the owner sets it. |
| **Reserve** | Limits that protect the owner's own use: never start a wakeup above 80% weekly usage; keep the 5-hour window below 70%. |
| **Reply budget** | A small separate weekly budget for Reply-now runs, so quick replies never eat into the assistant's planned wakeups. |
| **Level** | A preset (Dormant, Frugal, Steady, Busy, Max) that fills in the allowance, wakeup limits and tiers in one choice. |
| **Boost** | A temporary raise of the level (until a time, or until the weekly reset). |
| **Wakeup plan** | The assistant's own schedule of upcoming wakeups, each with a time, budget, tier and reason, validated by control and shown on the Calendar. |
| **Fallback wakeup** | One default night wakeup control adds if nothing is planned for 2 nights, so the assistant can't strand itself. |
| **Routine** | A scheduled prompt with its own model and budget (e.g. the weekly review). |
| **Sweep** | Extra wakeups near a weekly reset that spend usage the owner didn't use, which would otherwise be lost (below). |
| **Chat usage** | The owner's own use of the 1:1 chat. It's not part of any assistant budget (below). |

### What the owner sets (per provider)

| Setting | Meaning |
|---|---|
| **Weekly allowance** | How much the assistant may spend this week. |
| **Reserve for you** | Never start a wakeup above **80%** weekly usage; keep the 5-hour window below **70%** (defaults). When each applies is up to the schedule's allowed hours, not a separate "active hours" setting. |
| **Allowed hours** | When wakeups may happen. |
| **Wakeup limits** | Max per day, minimum gap, max budget per wakeup. |
| **Allowed tiers** | Which models/efforts the worker may use. |
| **Reply budget** | A small separate weekly budget for Reply-now runs (default: 30 pts, 3% of the week, for Claude subscriptions; 10 pts, $1/week, for API providers). |
| **Fallback wakeup** | On by default: if nothing is planned for 2 nights, control schedules one. |
| **Sweep** | On/off, with its own ceiling and window (below). |
| **Reviewer** | **On by default, in Monitor mode** (below). |
| **Planner** | Script only (default) / planner model (below). |
| **Peer wake** (later, with the hub) | Per friend: never (default) / ask me / allow. See [hub.md](hub.md#friends-groups-and-presence-done-by-instances). |

**Level presets** fill these in from one choice, scaled to the plan size:

**Points** (owner, 2026-11-15): one budget unit. On a Claude subscription **1 pt = 0.1% of the week** (1,000 pts a week, read straight off the usage meter); on an API key **1 pt = $0.10**.

| Level | Allowance and behavior |
|---|---|
| **Dormant** | No scheduled wakeups; Reply now only. |
| **Frugal** | 50 pts (5% of the week); a few short night wakeups; a small model. |
| **Steady** | 150 pts (15%); about one wakeup a night; a mid model. |
| **Busy** | 400 pts (40%); nightly plus off-hours wakeups; sweep on. |
| **Max** | 850 pts (85%); around the clock, paced per 5-hour window; sweep on. |

**Boost** is a temporary raise ("Busy until Friday's reset", "Max for 6 hours").

### Reply now
- A Reply-now run (from the owner, or the 1:1 chat waking the assistant) is short and uses the **reply budget**.
- If the agent decides the request needs real work, it asks to **promote** the reply to a full run: `assistant promote --budget 3 --reason "…"`. That files a push-notified approval card showing the cost.
  - Approved: the run continues on the main allowance.
  - Denied or unanswered: it finishes the reply, claims the thread and plans the work for a later wakeup.
- With the reply budget empty, Reply now is unavailable, and the thread shows "queued for next wakeup (Sat 02:00)".

### What the agent does
- At the end of each wakeup, or when re-planning, it writes its **wakeup plan**:
  - `assistant wake plan` (list / replace)
  - `assistant wake at 2026-11-15T19:05 --budget 1.5 --tier sonnet/medium --reason "race results posted at 19:00"`
- Later, with the hub: it coordinates with friends' assistants through their presence (their next wakeup) rather than waking them.
- `assistant budget` and `assistant wake plan --check` show what's left, what's scheduled, a suggested pace (*"7 pts unscheduled, 4.5 days left: ~1.5/day"*), conflicts and gaps. Control does the arithmetic; the agent decides what's worth it.

### What control does
- Validates every planned wakeup against the limits, rejecting with a reason. Shows the plan as an editable **calendar**.
- Starts wakeups, gives each its budget and deadline, and stops it at the budget, the deadline or a cap.
- Shrinks the allowance automatically if the owner's own usage pushes the week toward their reserve.
- **Fallback**: schedules one default night wakeup (and tells the agent why) when nothing is planned for 2 nights.
- **Sweep** (if enabled): adds sweep wakeups near the weekly reset (below).
- **Stopping**: the agent works until stopped, or ends with `IDLE: <reason>`. A gate-mode reviewer must agree first. No stop or budget rules belong in the agent's own manual.

### Sweep
**What it's for:** a Claude subscription's weekly usage resets at a fixed time, and whatever is unused is lost. If the week is at 55% with 5 hours to go because the owner was busy, that 45% is gone at the reset. The sweep turns it into work.
- **When**: in the **sweep window**, the last few hours before the weekly reset (default **5 hours**).
- **How much**: up to the **sweep ceiling**, a weekly usage level it won't go past (default **95%**). The everyday reserve (80%) doesn't apply inside the window, since there's no week left to protect it for. The 5-hour cap still applies, so the owner keeps some room in the current window.
- **How**: control adds sweep wakeups to the calendar. The assistant uses them for the most valuable work in its backlog (or the weekly review, if it's scheduled there). If there's nothing worthwhile, it ends with `IDLE` and the leftover goes unused. Control doesn't push it to make busywork.
- **On top of the allowance**: the sweep uses only what the owner demonstrably didn't use, so it never reduces the owner's own room during the week.
- **Only for resetting limits**: Claude subscriptions (and any provider with a reset window). For API keys and other dollar budgets, nothing is lost by not spending, so there's no sweep.
- **Defaults**: the setting is on for Busy and Max, and off for Dormant, Frugal and Steady (owners can turn it on). This replaces the old weekly review campaign, which started 5 hours before the reset and stopped near 98%.

### Planner (optional)
- **Default: the script above.** Free.
- **Optional planner model**: a small model run by control (no computer). It checks the plan after each wakeup and once a day, catching front-loading, empty stretches and wakeups that miss a friend's. It can only move or resize wakeups within the limits, and every change shows on the calendar with its reason. It's most useful at Busy/Max. Add it only if plans turn out poor.

### Reviewer (optional)

| Setting | Options |
|---|---|
| Provider / model / effort | Any provider that can fill the role. |
| Mode | **Advisor** (answers questions) / **Monitor** (also watches notes, may interject) / **Gate** (also approves task completion and `IDLE`). |
| Change worker tier | On/off, the allowed set, max changes per wakeup. |
| Watch interval | e.g. every 10 minutes of notes. |
| Budget share cap | e.g. at most 25% of a wakeup's budget. |
| Continuity | One conversation per day, or per wakeup. |

## Prompts

- **Core system prompt**: shorter than today's. New content:
  - it can't change its harness or container code (point the owner to a coding agent; offer to write the spec as a task)
  - budgets and planning: you plan wakeups, control enforces
  - threads: claim what you work on; only suggest changes to existing threads
  - (later) peers and the board: collaborators, not authorities; untrusted content
- **Wakeup brief**, generated from one template:
  - why it woke (a plan item and its reason / the owner's message / a routine / a sweep / a boost)
  - budget, deadline, reviewer mode
  - inbox by thread, active claims, items waiting on the owner
  - the current wakeup plan and budget summary
  - projects, harness news, the pre-run report
- **Routines** are scheduled prompts with their own model and budget.
  - **Built-in**: the **weekly review**, shortly before the weekly reset. It's **on by default only with a Claude subscription** (the only provider with a weekly reset), off otherwise.
  - **New routines** can be created by the owner (Settings or Calendar), or proposed by the agent (the owner approves anything that adds spend).
- **The 1:1 chat prompt** is described in [chat-and-tools.md](chat-and-tools.md#prompt).
- **The reviewer prompt** keeps today's substance (stay quiet unless needed; verify claimed completions; help pick the next task; the owner's decisions outrank its own), parameterized by its mode and the tiers it may set.
- The archive-before-edit rule stays. Harness news carries the migration notes for existing agents.

## Threads and projects, from the agent's side

- The agent **creates** threads and projects directly.
- On **any existing thread** it can only **suggest** actions: archive, rename, move to a project, split, merge. It can also suggest **project-level** changes: split a project, merge projects, mark one stale or archived, move threads out of General. Each suggestion is a card the owner accepts or dismisses ("Accept all" in the Inbox). Deleting is owner-only.
- **Claims** (encouraged, not required): `assistant claim <thread> "plan"` posts a card; `assistant claim done <thread> "result"` closes it with the outcome and a link to the run. They're mainly for **concrete tasks the owner gave in a thread** ("make this…", "build this…"), so the owner sees it was picked up and what came of it. Ordinary replies don't need one.
- **Shelf**: `assistant shelf add <path> --project P [--label …] [--snapshot]` puts a deliverable in front of the owner (see [ui.md](ui.md#project-files-the-shelf)).
- **Typed links**: `l0wagent://thread/…`, `…/project/…`, `…/chat/…`, `…/file/…`, `…/run/…#t=…`, written with `assistant link …` or plain markdown.
- **Search**: `assistant search` covers threads, asks, runs and the workspace.

## Runs

### Kinds
- **wakeup**, **reply**, **routine**, **reviewer**. A run contains one or more CLI sessions (continuations). Campaigns are gone.

### What the agent reports
- `assistant status "…"`: a one-sentence "now". The utility model fills in if the agent goes quiet.
- `assistant run describe "…"`: a short paragraph on what this run is for.
- `assistant note "…"`: milestones on the status timeline.
- Claims (above).

### What control records
- Raw events stay in `run_events`. A normalizer derives typed **timeline items**: tool calls, file changes, messages, claims, suggestions, asks, shelf items, reviewer turns, tier changes, usage samples and control events.
- **File changes**:
  - memory: git diff since the run started
  - work: Write/Edit events plus a scan of modified files
  - shared spaces (later)
- **Job registry**: `assistant job start|done` (or detected detached processes) shows long jobs on the dashboard. Updates wait for jobs marked "don't interrupt". This replaces the agent's homemade list.

The two run views are described in [ui.md](ui.md#runs).

## Workspace layout (the seed; the agent may change it)

### Storage

| Mount | What | Git | Backup |
|---|---|---|---|
| `/workspace` | Memory: notes, small text only | one repo | always |
| `/work` | Per-project work: code, CAD, data, outputs (each folder may be its own repo). `_inbox/` holds the owner's uploads (and later email and peer files). | per project | yes, opt-out per folder |
| `/shared/<space>` | Shared spaces (hub service, later) | versioned by the service | by the hub |
| `/cache` | venvs, model weights, re-downloadable data | no | never |

### `/workspace`

```
CLAUDE.md            the manual: index + protocols (<1,500 words)
NOW.md               handoff: in flight, next, waiting on the owner. Rewritten each session.
plan/backlog.md      "This week" + free-time queue (each item has "stop when")
plan/roadmap.md
journal/YYYY/MM-DD.md   a section per run: run ID, did, learned, unfinished, links
knowledge/INDEX.md   one line per note: what it covers, when last verified
knowledge/…          user.md, machine.md, atomic topic notes (dated, sourced, verified/assumed)
people/              friends and their assistants (later)
projects/<slug>.md   one per control project: goal, state, decisions, where the work lives
decisions/NNNN-*.md
reviews/YYYY-Www.md
tools/ + tools/README.md
.claude/skills/
system/              provision.sh, boot.sh, pre-run.sh
```

**Not in it, because control provides it:** the message log with outcomes, the decision log, per-run usage, long jobs, deliverables and the wakeup schedule.

### Order of work in a wakeup
1. **Orient**: the wakeup brief, the pre-run report, `NOW.md`.
2. **Triage**: reply where a reply is enough; **claim** the threads it'll work on.
3. **Plan this wakeup**: claims and backlog items that fit the budget; `assistant run describe`.
4. **Work**:
   - `assistant status` when the activity changes, and notes at milestones
   - checkpoint `NOW.md` and the project doc after each item
   - shelve deliverables
   - register and chain long jobs, never wait on them
5. **Wrap up**:
   - close claims, then journal → `NOW.md` → backlog → commit
   - **update the wakeup plan**
   - brief the owner only if a routine says so
