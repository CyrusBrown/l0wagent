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
| 26 | Home's budget card assumes points. | **Until M4:** a "Claude usage this week" card with the provider's weekly and 5-hour %, in the budget card's style. **M4:** the points card (left of the allowance, you · your chats · the assistant), with a Calendar link. |
| 27 | Threads have no owner actions (the agent's suggestions are the only way to rename, move or archive). | **M2:** a small ⋯ menu in the thread header: Rename, Move to project, Pin, Archive. |
| 28 | The Secure input card has only Save. | **M2:** a Decline button too, so the owner can refuse without waiting for the agent to withdraw. |
| 29 | Tools & Secrets copy says the assistant reads secrets "through tools, never as text". | **M2:** the copy describes what's built: secrets are in the agent's environment and redacted from everything shown. Revisit when brokered tools arrive (M6). |
| 30 | New offers "1:1 chat" and "Assistant · General". | **M2:** 1:1 chat is shown but disabled until M6; `?project=` starts the thread in a project ("New thread in project"). |
| 31 | The Reply-now line shows a cost and reply points. | **Until M4:** just "Reply now" (no reply budget yet). **M4:** "Reply now · up to 6 pts (30 reply pts left)": the most a reply run may spend, since its cost is only known afterwards. |
| 32 | The level picker has four levels. | **M4:** five, with Dormant first ("Reply now only"), in the same card style. |
| 33 | Budgets & levels shows its fields as read-only chips. | **M4:** each chip opens an inline editor (numbers, allowed hours as "22:00 – 08:00, …", tier checkboxes); overridden fields get a small "custom" tag and "Use the level's". Added rows: Weekly allowance, Allowed tiers, Reserve for you. "Protected hours" isn't a separate setting: it's the complement of the allowed hours, drawn hatched on the Calendar. |
| 34 | Boost is one button, "Double this week's allowance for 24 hours". | **M4:** Boost raises the level one step (the plan's meaning), "Until the reset" or "Boost for 24 h"; while active, the box says until when and offers "End boost". |
| 35 | The Calendar has no routines list and no editor. | **M4:** a Routines section at the bottom of the side panel (toggle, delete, "+ New" form) and a small dialog for a wakeup (time, budget, tier, reason; Cancel wakeup). Planned wakeups drag to another time or day in 15-minute steps; the sweep window is a dashed outline. |
| 36 | Providers, Roles, Reviewer and Standing instructions tabs. | **M4:** Providers (cards with status, write-only Anthropic API key form), Roles (the mockup's table with selects), Reviewer (mode cards like the level picker, then the field grid), Standing instructions (a textarea). |
| 37 | No offline state, no update notice, an empty Notifications tab. | **M5:** a thin bar under the top bar when offline or reconnecting (with the outbox count); messages written offline show as pending bubbles, "Waiting for connection (on this device)"; an "Update available" banner with the release notes (dismissable per version); Settings → Notifications (this device's push, per-event switches, quiet hours, ntfy), all in the mockup's card and switch styles. |
| 38 | The wizard's Provider step offers only the Claude login; Providers has only the Anthropic API key. | **M6 (opencode Go):** an "opencode Go" card under the Claude card in the wizard ("Use opencode Go" → key field), in the provider-card style; Settings → Providers gets an opencode Go key section and, on its card, the model behind each tier (Small/Medium/Large) as editable fields; Roles' model select shows the mapped model next to the tier name. |
| 39 | The chat screen's "Wake the assistant · ~2 pts" and the composer's model picker; no tool details, stop button or period settings. | **M6:** the header has Move to the assistant… (title + project), Ask the assistant (thread + message) and Pull in a thread, as small popovers in the menu style; "Wake the assistant" appears once the chat continued in a thread (it's Reply now there). The quiet tool line expands to each call and its result. While a reply streams, Stop replaces Send. The model picker lists tiers (with Go's mapped model names) and, with several providers, the provider. Settings → Budgets gets a "Budget period" field (week/month, reset day, caps) for key providers, and "this week" labels follow the period. |

## Product decisions shown by the mockup's data

| # | Mockup | Plan | Resolution |
|---|---|---|---|
| 14 | Levels **Frugal / Steady / Busy / Max** with fixed pts per week. | Dormant / Light / Standard / High / Max. | **Decided (owner, 2026-11-14): Dormant / Frugal / Steady / Busy / Max.** Plan renamed; the rough shares stay placeholders until points are defined. |
| 15 | Everything in **pts**, one unit across providers. | Per-provider native units. | **Decided (owner, 2026-11-15): points.** 1 pt = 0.1% of a Claude subscription's week (1,000 pts a week), or $0.10 on an API key. Levels: Dormant 0, Frugal 50, Steady 150, Busy 400, Max 850 pts a week (the plan's shares, not the mockup's 40/120/250/500); reply budget 30 pts. |
| 16 | Permissions offer **Allow for project / Allow once / Deny**. | Yes/no with actions. | **Decided: approvals carry a scope** (once / project / always), offered per permission. Already in the schema (`permissions.scope`) and API. |
| 17 | Sweep = scheduled "tidy threads" slots. | Sweep = leftover usage before the reset. | **Decided: the plan's meaning**, now specified in [agents-and-runs.md](../../docs/plans/agents-and-runs.md#sweep) (last 5 h, up to 95%, Claude subscriptions only). Fixtures use it. |
| 18 | 1:1 chat "uses the chat budget". | Chat is the owner's own usage. | **Decided: no chat budget by default** (optional cap), usage shown as its own line. The API's `BudgetSummary` has `used_by_owner`, `used_by_chat` and `used_by_assistant`; chat copy reads as owner usage, not "chat budget". |
| 19 | Setup offers **Anthropic and OpenAI**; the reviewer uses OpenAI. | Claude (subscription or API) and opencode Zen first. | **fixture** unless you want OpenAI early. |
| 20 | Fastmail preset, Home Assistant, a Telegram bot in Connected apps. | GitHub preset only at first; connected apps are MCP clients. | **fixture**. |
| 21 | Agent web page on port 8080. | Port 8787. | **fixture**. |
| 22 | Routines "Weekly digest" and "Inbox triage". | Built-in "weekly review". | **fixture**: rename to Weekly review. |
| 23 | `/workspace` holds `projects/ people/ journal/ memory/`; uploads in `inbox/`. | The workspace seed; uploads in `_inbox/`. | **fixture**. |
