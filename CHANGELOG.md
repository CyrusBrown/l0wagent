# Changelog

Each release's section becomes its GitHub release notes, which the app shows in the
"Update available" banner. Versions follow the milestones until 1.0.

## Unreleased

- Hubs: when your hub offers the faster `cloudflared` connection, the tunnel container uses it (Settings → Hub,
  on by default) and falls back to the normal tunnel by itself if it stops working.
- Hubs: the join screen previews your hostnames as you type a name, and a name-bound invite fills in the name and locks it.
- Fixed: runs were charged far too many points. Claude Code reports a conversation's running total, and a run resumes its
  conversation for every step (the reviewer keeps one a day), so each step was charged everything before it too. Each
  turn now counts only what it added. Points recorded before this update stay as they were.
- Fixed: over a hub, chat replies could stop after their first words until a refresh (the live stream now tells
  Cloudflare not to compress it, which held small updates back).
- Fixed: push notifications never reached iPhones, iPads or Safari. Apple refused them because of the made-up contact
  address they were signed with; they're now signed with your public URL (your hub hostname).
- Fixed: a bare `l0wagent://thread/20` in a chat or agent message now shows as a link ("thread #20").
- A running run has a "+10 pts" button beside Stop to give it more points.
- After an update, while the agent computer reinstalls its tools, runs wait with a note ("still setting up") instead of
  failing, and Settings and Home say it's setting up instead of "unreachable".
- Fixed: the authenticator app's QR code didn't show. Edge builds now report a version a hub can compare
  (`0.8.0-edge.<commit>`), so a hub no longer turns them away as too old.

## 0.8.0

- Extensions (SDK v1): put an extension in `extensions/<name>/` next to `compose.yaml` and switch it on in
  Settings → Extensions (or `./l0w extensions …`). They can add pages, sidebar sections, project tabs,
  Inbox cards, file previews, settings, routines, integration presets, prompt text and `assistant ext`
  commands. A broken one is switched off with its error shown. Guide: `docs/EXTENDING.md`; example:
  `extensions/examples/plantlog`.
- "Use the whole budget" (Settings → Advanced, on by default): a wakeup keeps working on the backlog
  instead of stopping when its planned work is done, and is told to carry on (twice by default) if it
  stops early; in Gate mode the reviewer's default answer to ending early is no.
- Waiting for your reply: each reply restarts the wait, the wait no longer counts against the run's
  time limit, and a run may wait for up to 10 replies (was 3).
- Settings → Advanced: the wait length, time limits, Reply-now and Wake-now budgets and other tuning
  knobs; the 1:1 chat can change them too.
- Standing instructions have a second box only the reviewer reads.
- The reviewer orients itself at the start of each conversation (the assistant's workspace, your recent
  messages, open claims) and hands the assistant concrete next tasks; the assistant must tell it when it
  starts and finishes each piece of work.
- More settings in Settings → Advanced (and through the 1:1 chat): the reviewer's Gate rounds, the
  fallback wakeup's hour and how many empty nights trigger it, how far ahead wakeups may be planned,
  brief list sizes, open asks per run, ask notifications per hour, upload, snapshot and folder-download
  limits, chat steps and reply length, the integration timeout, the lost-contact timeout and sign-in
  history retention.
- Budgets → level limits: changes are a draft with Save and Discard, the "You customized…" banner is
  gone (a "Modified from <level>'s defaults · Reset to default" line replaces it), and the sweep
  window can be changed.
- Enter sends in every message box, phones included; Shift+Enter is a new line.
- Long jobs past their estimate say so, instead of "expected in 1 minute".

## 0.6.0

- `./l0w update` no longer stops when the registry lacks one of the images (issue #2): it keeps a copy
  already on the machine and only stops, before changing anything, if an image is nowhere. It also
  says when the folder's checkout isn't the release the images come from, and how to match it.
- This release publishes the chat-runtime image as `:stable` (0.5.0 predates it).
- `./l0w migrate --from OLD_DIR` moves the old assistant into this instance.
- Runs no longer report "Lost touch with the agent computer" while a session is just quiet, and a
  quiet run can't be given up on after 10 minutes (issue #1): the runner sends a heartbeat on a quiet
  log, and an idle reconnect isn't counted as lost contact.
- The 1:1 chat: on an Anthropic API key or opencode Go, with tools that read your assistant's threads,
  runs and files; it can attach files, propose settings changes and hand work to the assistant (you
  approve each on a card), and answers how-to questions from a built-in guide.
- Monthly budgets with spending caps (opencode Go's default); opencode providers' models are a list
  you pick from the catalog, and any model can be used in a chat.
- The UI matches the mockup more closely (layouts, type, spacing, colors, motion); General threads
  overflow to their own screen; settings save as you change them; the reviewer's mode is a slider;
  providers are added from a searchable popup; a copy button for files.
- The app's own dropdowns replace the browser's native selects, and Escape closes popups and menus.
- Custom date and time pickers replace the system ones in the Calendar and notification quiet hours.
- Settings → Appearance has a 24-hour or 12-hour time setting (kept in this browser).
- Threads: an empty thread says so, the header stays put while you scroll, the project files sidebar
  no longer overflows and can be resized, and a file's copy menu is on the shelf too. The security
  card no longer offers "+ Add comment". The Files copy popup no longer overflows the window.
- A Network page (egress rules and the request log), also a "Network rules" tab in Tools & Secrets.
- Secrets have scopes: all projects, one project, one routine, or "Integrations only" (never in the
  assistant's environment). The reviewer, chat and connector never get values.
- Brokered MCP integrations: connect a remote MCP server (control holds its token), with a per-tool
  policy (Allowed, Ask or Blocked; Ask tools make a permission each time) and a GitHub preset.
- Guided GitHub setup from the chat: a secure card takes the token and connects it, and the chat
  only hears the outcome.
- The MCP connector at `/mcp`: OAuth 2.1 with a consent screen, connected apps (each with its own
  chat, revocable) and a downloadable skill for the outside model.
- 1:1 chats on the Claude subscription, through a chat-runtime sidecar that control calls per turn.

## 0.5.0

First friends release (M5).
- Install with `./l0w install`; update with `./l0w update`, which waits until the assistant is idle.
- The web app is installable, works offline (cached threads and an outbox), and sends push notifications.
- The `l0w` CLI covers what the old `ops` script did, for you or a coding agent on the host.
- No Claude subscription needed: the assistant can run on **opencode Go** (open models through
  opencode). Choose it in the setup wizard or add the key under Settings → Providers.

## 0.4.0

Budgets, wakeups and runs (M4): points, levels and Boost, the assistant's own wakeup plan with
fallback and sweep wakeups, routines with the weekly review, the reviewer, long jobs, the Calendar.

## 0.3.0

Files (M3): the file browser, project shelves with live items, previews, the assistant's web page.

## 0.2.0

Conversations (M2): projects, threads, asks, claims, suggestions, Reply now, secrets.

## 0.1.0

The core platform (M1): accounts, runs that survive restarts, the usage gate, egress.
