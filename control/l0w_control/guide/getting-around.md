# Getting around the web app

The top bar: **Conversations** (threads and projects), **Inbox** (what's waiting on you),
**Runs**, **Files**, **Tools** (Tools & Secrets), and **More** (Calendar, Network). The status chip on the right shows what the assistant is doing and the
budget; it opens Runs, and **Stop** ends the current run. The sliders icon opens **Settings**.
**+** (or the N key) starts a thread or a 1:1 chat; a chat on the Claude subscription needs the chat runtime installed. **Search** (⌘K or Ctrl-K) finds threads,
messages, projects, files and runs, and runs actions.

- **Home**: what's waiting on you, recent messages from the assistant, what it's working on,
  upcoming wakeups, the budget, long jobs and a system line.
- **Conversations**: the sidebar lists **General** (threads in no project; the latest three, and
  `···` opens all of them), **Projects** (expand one to see its threads; the `›` opens the
  project page) and your **Chats**.
- **Inbox**: questions, permissions, tasks, suggestions, promote requests, security alerts and
  cards from extensions, grouped by kind. **History** lists past decisions and is searchable.
- **Runs**: every run with its budget and status; open one for its summary, then **Open full
  timeline** for every step. **Long jobs** lists background work the assistant registered.
- **Files**: the assistant's storage: `/workspace` (its memory, a git repo) and `/work` (project
  work). Previews, history and diffs, **Copy** (contents or path), **Download**, **Add to shelf**.
- **Calendar**: planned wakeups, routines and protected hours for the week; **+ Add wakeup**.
- **Tools & Secrets**: four tabs. **Secrets** (write-only values, with scopes), **Integrations**
  (remote MCP servers such as GitHub, with each tool Allowed, Ask or Blocked), **Network rules** (the
  same rules and request log as the Network page) and **Connected apps** (outside chat apps that
  connect to your assistant, and can be revoked).
- **Settings**: Appearance (light, dark, system, and 24-hour or 12-hour times; the time choice is kept in this browser), Budgets & levels, Providers, Roles, Reviewer,
  Standing instructions (one box for the assistant, one only the reviewer reads), Account (password,
  2FA with ten one-time recovery codes, sessions and devices: revoke any of them, or make a device
  token for the `l0w` CLI, a script or another app with a scope: everyday work (no account, 2FA,
  device, secret, connected-app or settings changes), read only, or full access; it's shown once), Notifications, Extensions, Advanced (fine-tuning:
  how long a run waits for your reply, time limits, Reply-now and Wake-now budgets, sign-in and log
  retention) and System (self-test, storage and free disk space, scheduler problems).
- **Extensions** add their own pages (in More), sidebar sections, project tabs, Inbox cards and file
  previews; see "Extensions" in the files and tools page.

The app works offline for reading what you've opened, and thread messages, answers and tasks you
write offline are sent when the connection returns; the 1:1 chat needs a connection. Anything the
server refuses stays in the outbox with the reason, and **Retry** or **Discard**.

On a phone the app has a tab bar at the bottom (Home, Chats, Inbox, Runs, More); the conversation
list opens from the menu button at the top left. Add it to the home screen (it needs HTTPS for
notifications; see the install guide).
