# Chat and tools

The 1:1 chat, the connector for outside chat services, and Tools & Secrets. Part of the [overhaul plan](README.md).

## The 1:1 chat

A normal, instant LLM chat that also knows the owner's assistant. It has no computer. The server stores and syncs chats. **Chats are independent**: no memory across chats; what a chat knows comes from the assistant's data, through its tools.

### Where the model call runs
- **API providers**: control runs the tool loop itself. The tools are plain functions, so there's no shell anywhere.
- **Subscriptions whose sanctioned client is a CLI** (Claude Code): a **stateless chat-runtime sidecar**. It has no volumes, no secrets and no workspace, and can reach only the provider (through egress) and control's chat endpoint. It's a jail, not a computer. This keeps a model with built-in shell tools out of control, which holds every secret.

### Tools
- **Read**: projects, threads, runs, the shelf, budget and status, the wakeup plan, search, and the agent's workspace (read-only, on demand, never preloaded).
- **Write**: create threads and projects; post to a thread as the owner (labeled "via chat").
- **Wake the assistant**: Reply now, or an extra wakeup, within the budget rules, showing the cost.
- **Cards for anything sensitive**: secrets (secure input), approvals, settings and budget changes are proposed as cards the owner clicks. The chat can't do them itself.
- Provider-native web search when available.

### Hand-off
- **"Move to the assistant"**: a new thread whose first message is the chat's summary, with the transcript attached. The chat shows "continued in #thread".
- **"Ask the assistant"**: a message into an existing thread.
- **Pulling a thread into a chat** as read-only context.

### Prompt
- It can't change the assistant's machine or the harness.
- Machine work is handed to the assistant.
- For harness or container changes, it tells the owner to use a coding agent and can draft the spec.

### Chat usage (budget)
- **The 1:1 chat has no budget of its own by default.** It's the owner's own use, like using claude.ai directly. It doesn't touch the assistant's allowance, its reply budget, or any wakeup's budget.
- It's **not gated** by the assistant's reserve either: the reserve exists to protect the owner's use, and this *is* the owner's use. The provider's own limits still apply (a Claude subscription's 5-hour and weekly caps).
- **Tracked separately**: the usage view shows three lines (you on other apps, your 1:1 chats, the assistant) so the owner can see what the chat costs.
- **Optional cap**: an owner can set a weekly chat cap per provider (useful with dollar-billed API keys). When it's reached, the chat says so and stops.
- **Waking the assistant from a chat** is different. That's assistant work, so it uses the **reply budget** (or, for a full run, needs approval like any promote).
- **Outside chat services** (Claude.ai, ChatGPT…) through the connector run on *their* model and account, so they cost the instance nothing. If they use the same Claude account, that use still counts toward the owner's weekly limit, as any claude.ai use does. Waking the assistant from them uses the reply budget.

## Connector for outside chat services (MCP)

Claude.ai, Claude Desktop, ChatGPT, opencode, Cursor and Claude Code act as the 1:1 chat through an **MCP server** on the instance. It has the same tools as the built-in chat, written once.

- **Teaching the outside model**: the server's MCP instructions and tool descriptions, plus a downloadable **skill** (or `AGENTS.md` snippet). It covers when to hand off, how to cite threads, and the "can't change the harness" rule.
- **Cards inside the outside chat**: where the client supports interactive tool UIs (MCP Apps), questions and approvals render inside the conversation. Elsewhere, the tool returns a link to the card in the owner's UI. Secrets always go through the owner's UI.
- **Links back**: web chat UIs often strip custom schemes, so tools return `https://<instance>/t/123`. That page offers "Open in app" (tries `l0wagent://thread/123`) and falls back to the web UI.
- **Reachability**: Claude.ai and ChatGPT connect from *their* servers, so the instance must be reachable from the internet (a hub, or Tailscale Funnel). Desktop and CLI clients on the owner's network don't need that.
- **Auth**: OAuth 2.1 against the instance (`/mcp`, `/oauth/*`), with a consent screen and scopes (read; write threads; wake). Each connected client is listed and revocable. Secrets and approvals are never available through it.

## Tools & Secrets

The current Secrets page becomes **Tools & Secrets**.

### Secrets
- Write-only, redacted everywhere, reads logged (as today).
- **Scopes**: secrets go **only to the agent (worker) by default**. The reviewer, the 1:1 chat and outside chat services never get secret values. Narrower scopes (per routine or project) are possible.
- The assistant can't create or change secrets. It asks with `assistant ask --secret NAME=description`, which shows a secure input card in its thread.

### MCP servers and integrations
- **Brokered remote MCP servers**: control proxies the calls, so credentials never enter the sandbox. OAuth flows are done by control.
- **Sandbox MCP servers** the agent installs itself, registered so the UI lists them.
- **Presets**: **GitHub only at first**, with least-privilege guidance and the egress rules it needs. More come later, and extensions can add presets.
- **Per-tool policy**: allowed / ask each time (a permission card) / blocked. Read-only and write tools are labeled.

### Getting help with setup
The 1:1 chat (or an outside chat through the connector) walks the owner through a setup:
1. It explains the integration and links the provider's token page, with the exact scopes to tick.
2. It shows a **secure input card**. The value goes from the browser straight to control, and the model only learns "saved GITHUB_TOKEN".
3. It runs a test call and shows the result.

A coding agent on the host can use `l0w secret set` (today's `ops secret set`).
