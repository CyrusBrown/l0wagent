# 1:1 chats

A 1:1 chat is a normal, instant chat with your own model that can also see your assistant: its
threads, projects, runs, files (read-only), budget and plan. Each chat is independent.

- Start one with **+** → **1:1 chat**, or **+** next to Chats in the sidebar. The model picker
  under the message box changes the provider and model for that chat.
- The chat can **start a thread** for the assistant or **post into one** (labeled "via chat"),
  **wake it** with Reply now (the reply budget), and **hand the conversation over**: it proposes a
  hand-off card (a new thread, a new project, or an existing thread; either the whole transcript
  or a hand-off message it wrote), which you approve.
- It can **help with settings**: it reads them and proposes changes (budgets, levels, limits,
  providers' models, roles, the reviewer, standing instructions, notifications) as a card listing
  each change, before and after. Nothing changes until you approve.
- **Secrets** are always entered on a secure input card; the chat never sees the value.
- **Pull in a thread** gives the chat a thread as read-only context.
- Chats cost your own usage, not the assistant's allowance. They work with an API key or opencode Go,
  and on the Claude subscription through the chat runtime (a separate, locked-down service that runs
  Claude Code with only the chat's tools).
