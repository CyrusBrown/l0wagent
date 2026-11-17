# Files, tools, notifications and the server

- **Uploads**: attach files to a message (or Files → Upload to a project); they land in
  `/work/_inbox/<project>/` for the assistant.
- **The shelf**: a project's Files tab lists deliverables; **Add to shelf** on any file (live,
  always current; or a snapshot, a copy as it is now).
- **Secrets** (Tools & Secrets): write-only values, redacted from everything shown or stored. Each
  has a scope: **All projects** (in the assistant's environment), one project, one routine, or
  **Integrations only** (never in the assistant's environment; only brokered integrations read
  it). The reviewer, the 1:1 chat and the connector never get values. The assistant asks for one
  with a secure input card.
- **Integrations** (Tools & Secrets): connect a remote MCP server such as GitHub. Control holds
  the token and makes the calls, so the assistant gets the tools without the token. Each tool is
  **Allowed**, **Ask** or **Blocked**. Every tool starts on Ask, except a preset's vetted read-only
  tools (a server's own "read-only" label isn't trusted). If a tool's description or inputs change
  after you allowed it, it goes back to Ask and says "Changed since you allowed it". An Ask
  tool creates a permission each time: **Allow once** lets that exact call through once, and
  **Allow always** makes the tool Allowed.
- **Connected apps** (Tools & Secrets): outside chat apps, such as Claude or ChatGPT, connect
  through `<your address>/mcp`. Each connected app has its own chat, where its tool calls and cards
  appear. **Revoke** ends its access. The skill download teaches the outside model how to use the
  tools.
- **Network** (More → Network, or the Network rules tab in Tools & Secrets): the assistant reaches
  the internet only through the egress proxy; model providers only during runs. Rules allow or
  block hosts (or allow read-only), and the request log lists what it asked for.
- **Notifications** (Settings → Notifications): web push on this device (needs HTTPS, e.g.
  Tailscale HTTPS) or an ntfy topic; approvals and new sign-ins notify by default; quiet hours.
  ntfy sends titles only unless you turn previews on, and takes an optional access token: on the
  public ntfy server anyone who knows a topic's name can read it.
- **Updates**: a banner shows a new release; apply it on the server with `./l0w update`, which waits
  until the assistant is idle. While it waits, the banner says what it's waiting for and has
  **Cancel update**; a stuck update clears itself after ten minutes (or `./l0w drain off`). `./l0w`
  also has status, logs, inbox, secrets, `2fa-reset` (lost your phone and recovery codes) and more
  for the host.
- **Self-test** (Settings → System) checks the assistant's isolation.

The 1:1 chat can walk you through connecting an integration such as GitHub. It explains the steps, links the provider's token page with the permissions to choose, and shows a Connect card with a secure input. The token goes straight from your browser to control, is saved for integrations only, and the chat never sees it.

## Extensions

Extensions are add-ons you (or a coding agent) put in the `extensions/` folder next to
`compose.yaml` on the server, then switch on in **Settings → Extensions** (found ones start off).
They can add pages (under More), sidebar sections, project tabs, Inbox cards, file previews,
settings, routines, integration presets, text in the assistant's prompt, and commands for it
(`assistant ext …`). A broken one is switched off with its error shown; control keeps running.
Updates don't touch the folder; an extension's data and settings are kept. Extensions from git are
listed in `extensions.toml` and fetched with `./l0w extensions sync`. The assistant can't install or
change extensions: if it wants one it files a task with a spec. How to write one: `docs/EXTENDING.md`
in the repository (and the example, `extensions/examples/plantlog`).
