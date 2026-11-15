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
| 13 | Settings tabs Providers, Reviewer, Standing instructions, Account, Notifications, Extensions and System are empty. | Designed in the mockup's style as each milestone needs them. **M1:** Account (owner, password, 2FA with a QR code, passkeys note, sessions and devices) and System (status rows, self-test list styled like the wizard's). The other tabs show a dashed placeholder (the mockup's extension-tab style) naming the milestone that brings them. |
| 24 | No way to start a run by hand. | **M1:** a "Wake now" primary button on Runs, next to the title. |
| 25 | The setup wizard has six steps. | **M1** shows the steps that exist: Owner account, Timezone, Provider (the agent's Claude login status and how to log in), Self-test. Level joins in M4; Hub appears once a hub is advertised (#11). |
| 26 | Home's budget card assumes points. | **Until M4:** a "Claude usage this week" card with the provider's weekly and 5-hour %, in the budget card's style. |
| 27 | Threads have no owner actions (the agent's suggestions are the only way to rename, move or archive). | **M2:** a small ⋯ menu in the thread header: Rename, Move to project, Pin, Archive. |
| 28 | The Secure input card has only Save. | **M2:** a Decline button too, so the owner can refuse without waiting for the agent to withdraw. |
| 29 | Tools & Secrets copy says the assistant reads secrets "through tools, never as text". | **M2:** the copy describes what's built: secrets are in the agent's environment and redacted from everything shown. Revisit when brokered tools arrive (M6). |
| 30 | New offers "1:1 chat" and "Assistant · General". | **M2:** 1:1 chat is shown but disabled until M6; `?project=` starts the thread in a project ("New thread in project"). |
| 31 | The Reply-now line shows a cost and reply points. | **Until M4:** just "Reply now" (no reply budget yet). |

## Product decisions shown by the mockup's data

| # | Mockup | Plan | Resolution |
|---|---|---|---|
| 14 | Levels **Frugal / Steady / Busy / Max** with fixed pts per week. | Dormant / Light / Standard / High / Max. | **Decided (owner, 2026-11-14): Dormant / Frugal / Steady / Busy / Max.** Plan renamed; the rough shares stay placeholders until points are defined. |
| 15 | Everything in **pts**, one unit across providers. | Per-provider native units. | **Open: the owner decides how points are calculated before M4.** The M0 schema and API keep budgets unit-free (`BudgetSummary.unit`). |
| 16 | Permissions offer **Allow for project / Allow once / Deny**. | Yes/no with actions. | **Decided: approvals carry a scope** (once / project / always), offered per permission. Already in the schema (`permissions.scope`) and API. |
| 17 | Sweep = scheduled "tidy threads" slots. | Sweep = leftover usage before the reset. | **Decided: the plan's meaning**, now specified in [agents-and-runs.md](../../docs/plans/agents-and-runs.md#sweep) (last 5 h, up to 95%, Claude subscriptions only). Fixtures use it. |
| 18 | 1:1 chat "uses the chat budget". | Chat is the owner's own usage. | **Decided: no chat budget by default** (optional cap), usage shown as its own line. The API's `BudgetSummary` has `used_by_owner`, `used_by_chat` and `used_by_assistant`; chat copy reads as owner usage, not "chat budget". |
| 19 | Setup offers **Anthropic and OpenAI**; the reviewer uses OpenAI. | Claude (subscription or API) and opencode Zen first. | **fixture** unless you want OpenAI early. |
| 20 | Fastmail preset, Home Assistant, a Telegram bot in Connected apps. | GitHub preset only at first; connected apps are MCP clients. | **fixture**. |
| 21 | Agent web page on port 8080. | Port 8787. | **fixture**. |
| 22 | Routines "Weekly digest" and "Inbox triage". | Built-in "weekly review". | **fixture**: rename to Weekly review. |
| 23 | `/workspace` holds `projects/ people/ journal/ memory/`; uploads in `inbox/`. | The workspace seed; uploads in `_inbox/`. | **fixture**. |
