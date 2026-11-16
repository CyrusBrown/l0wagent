# UI

Everything the owner sees, written so mockups can be built from it. **The mockup ([design/mockup/](../../design/mockup/)) is the authority on UI and styling and outranks this file**; differences and how they were settled are in [REVIEW.md](../../design/mockup/REVIEW.md). Layout details are suggestions; the behaviors are decisions. Part of the [overhaul plan](README.md). Data and API conventions are in [data-model.md](data-model.md).

## Stack
- **React + TypeScript + Vite**, a single-page app. The JSON API is described by OpenAPI, and TypeScript types are generated from it. Live updates stream over `fetch` (not `EventSource`, so the desktop app can send its token).
- Suggested libraries:
  - **TanStack Query**: its saved cache and paused-change queue give most of the offline outbox
  - **React Router** (or TanStack Router)
  - **Tailwind + shadcn/ui**
  - `react-markdown` + `rehype-sanitize`
  - **react-three-fiber** for the 3D viewer
  - a virtualized list for long threads and timelines
- No Next.js or server rendering. The app is built inside the Docker image, so nobody needs Node to run it.
- Mockups should be React too, so they carry over.

## Layout

**Sidebar**, top to bottom:
- **Home** and **Search** (also a command palette, below)
- **Assistant**
  - General (threads without a project)
  - projects, collapsible, each listing its threads (archived threads hidden behind a toggle)
- **Chats**: the 1:1 agent chats, optionally grouped by project
- **Hub** (later): friends, the board (read-only), services
- Footer: **Inbox** (with a count), **Runs**, **Calendar**, **Files**, **Tools & Secrets**, **Settings**

A **status chip** in the header shows asleep / working (with the "now" sentence) / next wakeup and the budget left, plus a Stop button while running.

## Home
What opens first. At a glance:
- the assistant's status: asleep / working (the "now" sentence and active claims) / next wakeup
- what's waiting on you (top Inbox items, answerable in place)
- recent assistant messages across threads
- upcoming wakeups and routines
- budget left this week, and **you vs. the assistant** usage
- long jobs running
- update or system notices

## Search and command palette
- **Search** across threads, messages, chats, projects, shelf items, runs (notes, about text) and the workspace, with filters by kind, project and date.
- A **command palette** (Ctrl/Cmd-K): jump to a thread, project, file or run, or start an action (new thread, Reply now, new chat, pause the assistant).

## Projects list
- All projects, with status (active / stale / archived), summary, unread count, open asks and last activity.
- Create a project, filter by status, and see archived projects and archived threads.

## Thread view
- **Messages** from the owner, the assistant, "via chat" (from the 1:1 chat or an outside chat service) and system notices.
- **Inline cards**:
  - **asks**: questions, permissions, tasks, answered in place
  - **claims**: "Planned in run #123: …", updating live to in progress / done / partial / dropped with a result line and a link to the run
  - **suggestions**: move / rename / split / merge / archive a thread, or split / merge / archive a project, with Accept and Dismiss
  - **promote requests**: "turn this reply into a full run, ~3 pts", with Approve and Deny
  - **security alerts**: "That was me" or Revoke, without the "+ Add comment" note the other cards have
  - **secure input**: a secret field that never goes through a model
  - file and link chips
- **Composer**: paste text and images, drag files, attach from the project's files, and `@project` / `#thread` / `/file` autocomplete for typed links.
- **Delivery indicator**: **queued by default**. It shows one of:
  - *Working now, sees it within a minute*
  - *Queued for next wakeup (Sat 02:00)*
  - *Dormant*
  - *Waiting for connection* (offline, on this device)

  **Reply now** shows its cost and what's left of the reply budget.
- **Empty thread**: "No messages yet. Write the first one below."
- **Header**: the thread header stays at the top while the messages scroll.
- **Project files panel**: a sidebar beside the thread listing the project's files; drag its left edge to resize it. It scrolls instead of overflowing the window.
- **Typed links** open in a side panel: threads, projects, chats, files, runs (at a timestamp). Links can be pasted or inserted from autocomplete, by you or the assistant.

## Project view
Tabs:
- **Overview**: summary, status (active / stale / archived), the agent's project doc rendered, links
- **Threads**
- **Files** (the shelf, below)
- **Asks**: the project's open items
- **Activity**: notes, claims, runs touching it
- later, **Members** (shared projects)

## Project files (the shelf)
- **Items**: label, source, mode, last updated, who added it (the assistant, or "from you").
  - **Live**: always the current file, so a script output stays fresh.
  - **Snapshot**: copied when shelved.
  - Live items can **keep versions** (on each change, up to N), with a version picker.
- **Uploads** (drag in, or attach in the composer) land in `/work/<project>/_inbox/` on the agent's computer (the only place control may write there) and appear as "from you". Uploads to General threads go to `/work/_inbox/`.
- **Previews**: markdown, code, images, PDF, CSV tables, audio, video, **STL/STEP/3MF in a 3D viewer**, HTML and notebooks. Extensions can add previewers.
- **Copy**: each shelf item has a copy menu (contents or path), as in Files.
- **Safety**: agent-made HTML/SVG renders in a sandboxed frame on a separate origin. That's a design constraint for the preview panel.

## Inbox
Everything waiting on the owner, filterable. A **History** tab shows what was decided before (answers, approvals, denials, done tasks, accepted suggestions), searchable.
- questions, permissions, tasks
- thread suggestions ("Accept all" available)
- promote requests
- new-device/session alerts
- later: friend requests, peer-wake requests, outbound emails awaiting approval, spend-breaker cards

Each item links to its thread.

## Files (the assistant's computer)
A read-only **file browser** for the agent's storage, beyond any one project's shelf:
- **Roots**: `/workspace` (its notes and memory), `/work` (project work), and later `/shared` (shared spaces). `/cache` is hidden.
- **Browse** folders with name, size, modified time and last change by which run; **preview** files with the same previewers as the shelf; **download** files or folders (zip).
- **Git-aware for `/workspace`** (and for project folders that are repos): file history and diffs, "changed in run #123".
- **Add to a project's shelf** (live or snapshot) from any file.
- **Upload** into a project's inbox folder.
- Read-only, except uploads. Symlink-safe, with agent-made HTML in the sandboxed frame.
- Markdown links between the assistant's notes work, so its notes can be read like a wiki.

## Long jobs
The job registry: what the assistant has running in the background (label, started by which run, expected end, "don't interrupt"), with its log tail. Shown on Home and as a tab on Runs. A job marked "don't interrupt" holds updates until it ends.

## Network
- **Egress log**: every request the assistant made, with host, method, status, bytes and blocked ones highlighted. Filters, and top hosts.
- **Rules**: block / allow / allow read-only, add and remove. Approving a permission that adds a rule shows up here.
- (This is today's Network page, kept as its own page, linked from Tools & Secrets.)

## The assistant's web page
Whatever the assistant serves on its web port (today's "Agent web"): opened in a panel or a new tab, in the sandboxed frame. It's linked from Home if the assistant has something running there.

## Calendar
- The assistant's **planned wakeups** (time, budget, model, reason), routines, the fallback wakeup, sweep slots, and your protected hours.
- Drag to move, click to resize or delete, add your own.
- A side panel shows budget left this week, what's scheduled, and usage split three ways: **you (other apps), your 1:1 chats, the assistant**. Sweep wakeups are marked as such.

## Runs

### List
One row per run: kind, why it woke, start time and duration, budget used vs. given, the "about" sentence, and status. Filters by kind and project.

### Simple view (default)
- **Header**: why it woke, level, reviewer mode, model/effort (with changes), a **budget gauge**, elapsed time and deadline, Stop.
- **Now**: one sentence.
- **About this run**: a short paragraph.
- **Active threads**: the claim cards, live.
- **Status timeline**: notes plus control events (resumed, model changed, reviewer interjected, ask filed, file shelved).
- **Changes**: memory files and lines (+/−), work files, shelved items, asks filed, threads and projects created or suggested.

### Detailed view
One unified, filterable, searchable, live timeline:
- tool calls (grouped, expandable), thinking (toggle)
- file diffs, messages, claims, suggestions
- **the reviewer conversation interleaved**
- model changes, usage samples, control events

Every item has a deep link.

## Chats (1:1)
- A normal chat layout with streaming replies, model picker (from the chat role's allowed set), paste and file attach.
- **Tool activity** shown compactly ("read 3 threads", "searched the workspace").
- **Cards** for sensitive actions, as in threads.
- **Actions**: Move to the assistant (pick a project) / Ask the assistant / Wake the assistant (shows cost). Once moved, the chat shows "continued in #thread".

## Tools & Secrets
- **Secrets**: names, descriptions, scopes, last read, read count. Values are write-only.
- **Integrations**: connected MCP servers and presets, with status, per-tool policy (allowed / ask / blocked) and "Set up with help" (opens a 1:1 chat).
- **Network rules**: egress allow and block rules, plus the request log (may stay its own page).
- **Connected apps**: outside chat services connected through MCP, with scopes and revoke.

## Settings
- **Budgets & levels**: the level picker with a plain explanation, per-provider allowances and reserves, sweep (on/off, ceiling, window), optional chat cap, allowed hours, wakeup limits, reply budget, fallback, sweep, Boost.
- **Providers**: add/login, status, usage source.
- **Roles**: worker / reviewer / replier / chat / utility, each with provider, model, effort and allowed ranges.
- **Reviewer**: mode, interval, budget share, tier-change rights.
- **Standing instructions** for the assistant.
- **Account**: password, 2FA, passkeys, **sessions and devices** (revoke).
- **Notifications**: per event type, per thread, quiet hours. **On by default**: anything needing your approval, and new-device logins. Everything else (assistant messages, failed runs…) is opt-in.
- **Extensions**: installed, enabled, errors. Extensions can add their own pages, sidebar sections, cards and settings panels, so leave room in the layout for them.
- **System**: updates, the self-test, host resources help.
- Later: **Hub**: membership, address, published services, peer policies and spend-breaker limits.

## Login and setup
- **Login**: username + password, then a 2FA code if enabled. Rate-limit and lockout messages, and a "new device" notice to your other devices.
- **First-run wizard**: create the owner, timezone, connect a provider (the login flow runs through the server), pick a level, optionally join a hub (later). It ends with the self-test result.
- **Update available** banner, with release notes and "update after the current run".

## Throughout
- **Empty states** that explain what goes there ("No projects yet: the assistant creates them as work grows, or create one").
- **Light and dark themes**.
- **Controls** are the app's own, not native ones. Dropdowns (`Select`) open a popup under the field, work from the keyboard (arrows, Enter, Tab) and close on outside click; date and time use `DateTimePicker` (Calendar, notification quiet hours). **Escape** closes the topmost popup, menu or dialog.
- **Time format**: Settings → Appearance switches every time between 24-hour (default) and 12-hour. It's kept per browser, like the theme.
- **Keyboard shortcuts** (palette, new thread, next unread, Reply now).
- **Unread counts** per thread, project and Inbox.
- **Extension slots**: pages, sidebar sections, project tabs, card and thread types, previewers, settings panels (see [server-platform.md](server-platform.md#extensions)).

## Mobile (installable web app)
- Thread view, Inbox and cards are designed for one thumb. The sidebar becomes a drawer.
- **Push notifications** need HTTPS with a valid certificate (a hub, or Tailscale HTTPS). iOS supports push for home-screen web apps. ntfy stays as a fallback for plain-HTTP installs.

## Desktop (Tauri)
- **Connect screen** on first launch: the server URL (LAN, Tailscale, or later a hub address; LAN discovery and hub lookup can fill it in), then username + password (+ 2FA). The app gets a revocable device token, stored in the OS keychain.
- **Why server-loaded**: a UI compiled into the app would drift from the server and need a signed desktop release for every UI change. Loading it from the server keeps them in step, and caching it locally keeps offline use.
- **The UI comes from the server, cached locally**: the app downloads the server's versioned UI bundle, serves it from a local app protocol, updates it when the server's version changes, and uses the cached copy offline. So the UI always matches the server, and native permissions are granted only to the local copy.
- **Native features**, used only inside Tauri: notifications (from the live stream while the app runs; web push isn't needed), a tray icon with the assistant's status, `l0wagent://` links, drag-and-drop to the shelf, a global shortcut, multiple windows.
- **Agent-made content** (HTML previews, the assistant's web page) still renders in a sandboxed frame from the server's separate files origin, which never gets Tauri's native permissions.
- **Distribution**:
  - CI builds unsigned installers (`.dmg`, `.msi`/`.exe`, `.AppImage`/`.deb`).
  - Mac: approve once in System Settings → Privacy & Security → "Open Anyway". Windows: SmartScreen → "Run anyway".
  - The app binary rarely changes, since the UI comes from the server. Its own updates use Tauri's updater, signed with a free Tauri key; whether those updates trigger the Mac prompt again is to be checked in phase 5.
  - Building it yourself stays documented.
  - The installable web app also works on desktop (Chrome/Edge "Install app", Safari "Add to Dock"): app window, dock icon and push notifications, with no installer.
- **Later, optionally**: the app could install and start a local Docker stack for friends without a server.

## Offline (web app and desktop)
- **Local cache** of projects, threads, recent messages, the Inbox, shelf metadata, and optionally files you opened.
- **Outbox**: new messages, new threads, answers to questions, tasks marked done/declined, drafts and uploads. Queued with client IDs, sent in order when the connection returns, with no duplicates.
- **Shown clearly**: *waiting for connection* (on your device) vs. *queued for next wakeup* (on the server).
- **Online only**: approving permissions, secrets, settings, budget changes and promote requests.

## Later, with the hub (see [hub.md](hub.md))
These screens will mostly come from **extensions** (first-party ones bundled with the image), so they plug into the slots extensions get: sidebar sections, pages, thread types and Inbox cards.
- **Friends**: list with home hub, presence (asleep / working / next wakeup), per-friend policies (wake, spend limits), group chats.
- **Board** (read-only for people): topics, posts with votes and files, a **Discussions** tab, and "Ask my assistant about this" on every post and discussion.
- **Services**: what your assistant publishes (with access levels and unpublish), the hub's services, and member services labeled as such. Open a service in a side panel.
- **Shared projects**: members, the group thread, shared spaces with versions and locks.
- **Email**: an Email thread, and outbound mails awaiting approval in the Inbox.
