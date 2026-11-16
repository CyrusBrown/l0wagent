# How the assistant works

The assistant is a long-running AI on its own computer (a container on your server). It wakes
up for **runs**, works through its threads, and writes everything it needs to remember into its
workspace; between runs it remembers nothing else.

- **Threads**: you and the assistant talk in threads, in General or in a project. A message
  you post is delivered at its next run, or right away with **Reply now** (a short run on its
  small reply budget). If a run is in progress, it sees new messages after its next step.
- **Wakeups**: the assistant plans its own wakeups within your budget and limits (allowed hours,
  how many a day, points per wakeup). If nothing is planned for two nights, control adds a
  **fallback** wakeup. You can add wakeups on the Calendar or press **Wake now** on Runs.
- **Asks**: when it needs you, it files a **question** (answer in place), a **permission** (allow
  for the project, once, or deny; e.g. a network rule or a setting), a **task** (something only you
  can do), or a **promote request** (turn a reply into a full run that spends the allowance).
  Asks never block it: it carries on and picks up your answer after its next step.
- **Claims** show which thread it's working on in a run. **Suggestions** propose tidying threads
  and projects (rename, move, split, merge, archive); you accept or dismiss them.
- **The shelf**: deliverables the assistant puts on a project's Files tab; live items always
  show the current file.
- **Long jobs**: background work that outlives a run, with its log.
- **The reviewer**: a second model that watches wakeups (Settings → Reviewer). Off, Advisor
  (answers when asked), Monitor (reads its notes and may interject; the default) or Gate (must
  agree before it stops or closes a task). It can move the assistant to another model mid-run.
- **Standing instructions** (Settings) open every run's brief.

What it can't do: change its own harness (control, the runner, its prompts, the containers),
approve its own requests, set secrets, or change settings and budgets. Changes to the harness are
done by you with a coding agent on the server; the assistant or the chat can draft the request.
