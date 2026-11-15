# Mockup review (M0)

Where the mockup (`mockup.bundle.html`, readable sources in `src/`) and the plan differ.

**Rule (owner, 2026-11-14): the mockup is the highest authority on UI and styling.** Where it differs from [ui.md](../../docs/plans/ui.md) on layout, navigation, screens, components or copy, the mockup wins and ui.md is updated to match. What the mockup's *mock data* implies about product behavior (budget units, levels, providers, run semantics) is not UI; those rows are marked **owner**.

Resolutions: **plan** = update the plan to match the mockup. **owner** = needs your decision. **fixture** = only the mock data changes (realistic examples that follow the plan); the UI stays as the mockup draws it.

## UI: the mockup wins

| # | Mockup | ui.md said | Resolution |
|---|---|---|---|
| 1 | **Top header nav**: Conversations, Inbox, Runs, Files, Tools, More ▾ (Calendar, Network, Web page); Settings is a gear icon. The sidebar appears only on conversation screens, is collapsible, and switches between the Assistant tree and the Chats list. | A permanent sidebar with a footer of pages. | **plan**. Extension pages go in More ▾; extension sidebar sections go in the conversation sidebar. |
| 2 | Home is reached via the logo; **New** (`+`, `N`) opens a composer that starts either a 1:1 chat or a General thread. | Home and Search in the sidebar. | **plan**. |
| 3 | Search is the ⌘K palette, filtered by kind. | Search with filters by kind, project and date. | **plan**: the palette is the search UI. |
| 4 | A **"waiting on you" tray** in the thread view (this thread / everywhere). | Not described. | **plan**. |
| 5 | A **Files side panel** in the thread view (the project's shelf). | Not described. | **plan**. |
| 6 | Settings has an **Appearance** tab (light / dark / match system). | Themes, no tab. | **plan**. |
| 7 | Settings budget fields include **Protected hours** (18:00–22:00), drawn striped on the calendar. | No separate active-hours setting; allowed hours do that job. | **plan** for the UI. Whether protected hours is its own stored setting or derived from allowed hours is a backend detail; the screen shows it either way. |
| 8 | Question cards don't show the default answer; "+ Add comment" on every card. | Questions always carry a default and an optional note. | **plan**: the default stays in the data (the agent needs it) but isn't shown. "Comment" in the UI is `note` in the API. |
| 9 | Question types add **slot** (pick a time) and **scale** (1–5). | Four kinds. | **plan**. |
| 10 | Run list shows kind and why as one label (Planned wakeup, Routine, Sweep, Promoted reply, Fallback). | Kinds wakeup / reply / routine / reviewer. | **plan**: the API keeps `kind` + `why`; the UI shows the mockup's label. |
| 11 | Setup wizard has a **Hub** step ("Skip for now"). | Hub join later. | **plan**: the step is drawn as in the mockup and appears once `/api/meta` advertises the hub capability. |

## Not in the mockup (designed in its style when needed)

| # | What | Resolution |
|---|---|---|
| 12 | **Mobile** layout (the mockup has a fixed 640 px minimum height; the nav doesn't collapse). | Design it in the mockup's style when building Thread and Inbox for phones; worth a mockup pass first. |
| 13 | Settings tabs Providers, Reviewer, Standing instructions, Account, Notifications, Extensions and System are empty. | Designed in the mockup's style as each milestone needs them (Account in M1). |

## Product decisions shown by the mockup's data

| # | Mockup | Plan | Resolution |
|---|---|---|---|
| 14 | Levels **Frugal / Steady / Busy / Max** with fixed pts per week (40 / 120 / 250 / 500). | Levels **Dormant / Light / Standard / High / Max** as % of the plan. | **owner**. The level picker's look follows the mockup; which levels exist is your call. |
| 15 | Everything in **pts**, one unit across providers ("42 of 120 pts left"). | Per-provider native units (% of week, $). | **owner**: what is a point? Needed before `budgets` is designed (M4); the M0 schema stores `runs.budget` unit-free. |
| 16 | Permissions offer **Allow for project / Allow once / Deny**. | Yes/no with actions. | **owner**: confirm approvals have a scope. The API already supports it (`scope`: once / project / always). |
| 17 | Sweep = scheduled "tidy threads" slots. | Sweep = spend leftover budget near the weekly reset. | **owner**: which meaning? |
| 18 | 1:1 chat "uses the chat budget". | Chat usage counts as the owner's own. | **owner**: is there a chat budget? |
| 19 | Setup offers **Anthropic and OpenAI**; the reviewer uses OpenAI. | Claude (subscription or API) and opencode Zen first. | **fixture** unless you want OpenAI early. |
| 20 | Fastmail preset, Home Assistant, a Telegram bot in Connected apps. | GitHub preset only at first; connected apps are MCP clients. | **fixture**. |
| 21 | Agent web page on port 8080. | Port 8787. | **fixture**. |
| 22 | Routines "Weekly digest" and "Inbox triage". | Built-in "weekly review". | **fixture**: rename to Weekly review. |
| 23 | `/workspace` holds `projects/ people/ journal/ memory/`; uploads in `inbox/`. | The workspace seed; uploads in `_inbox/`. | **fixture**. |
