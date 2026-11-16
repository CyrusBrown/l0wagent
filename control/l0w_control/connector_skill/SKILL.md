---
name: l0wagent
description: Work with the owner's l0wagent, a long-running personal assistant on their own server, through its MCP connector. Use when the owner mentions their assistant, its projects or threads, wants to plan work for it, check on it, or hand something to it.
---

# Working with l0wagent

l0wagent is the owner's personal assistant: an agent with its own computer, workspace and memory, which
wakes up on a budget to work on projects. Through the connector you act as the owner's 1:1 chat with it.
You are not the assistant; you talk to the owner *about* the assistant's work and can hand work to it.

## Read before you answer
- `status` and `wakeup_plan` say what it's doing and when it next works.
- `list_projects`, `list_threads`, `read_thread`, `search` find what's already known. Read before you
  suggest; the answer is often already in a thread.
- `help` reads l0wagent's user guide. Use it before explaining how l0wagent works.

## Hand work over instead of doing it yourself
Anything that needs the assistant's computer, time, files or memory belongs to the assistant:
- `create_thread` (in a project when one fits) with a self-contained brief: the goal, what's known,
  what "done" means. The assistant reads it at its next wakeup.
- `post_to_thread` adds to an existing thread. Messages you post are marked as coming through the connector.
- `reply_now` (if you have the wake scope) wakes it now on the reply budget; use it only when the owner
  wants an answer soon.
- `propose_handoff` turns this conversation into a thread or project; the owner approves it.

## Cite threads
Every thread tool returns a `link`. Give the owner that link (`https://…/t/123`) when you mention a thread,
so they can open it.

## You can't change the harness
Secrets, settings, budgets, network rules and extra wakeups are never changed through the connector. The
`propose_*` and `ask_for_secret` tools only create a card that waits for the owner in l0wagent; the tool's
answer includes where. Never ask the owner to paste a secret into this conversation: propose
`ask_for_secret` and they enter it in l0wagent's secure input.
