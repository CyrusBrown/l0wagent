# How the assistant works

The assistant is a long-running AI on its own computer (a container on your server). It wakes
up for **runs**, works through its threads, and writes everything it needs to remember into its
workspace; between runs it remembers nothing else.

- **Threads**: you and the assistant talk in threads, in General or in a project. A message
  you post is delivered at its next run, or right away with **Reply now** (a short run on its
  small reply budget). If a run is in progress, it sees new messages after its next step.
- **Wakeups**: the assistant plans its own wakeups within your budget and limits (allowed hours,
  how many a day, points per wakeup). If nothing is planned for two nights (Settings → Advanced → Wakeups), control adds a
  **fallback** wakeup. You can add wakeups on the Calendar or press **Wake now** on Runs. A
  wakeup can carry the assistant's **note** to itself (where to start, what not to redo); it opens
  that wakeup's brief and shows on the Calendar.
- **Emergency pause** (Settings → System): stops the current run and holds everything (wakeups,
  routines, Reply now, Wake now) for 1, 3 or 24 hours or until you resume. Wakeups that come due
  meanwhile are skipped, not saved up; when it ends, the assistant is told what it missed. A
  banner shows on every screen while it's on. The chat and the connector keep working.
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
  agree before it stops or closes a task). It can move the assistant to another model mid-run. At
  the start of each conversation (one a day by default) it reads the assistant's workspace (manual,
  handoff, backlog, roadmap, recent journal) and your recent messages, and keeps a ranked list of next
  tasks it hands the assistant when it finishes something, asks what's next, or wants to stop. The
  assistant must tell it when it starts and finishes each piece of work.
- **Standing instructions** (Settings) open every run's brief. A second box holds instructions only
  the reviewer reads.
- **Use the whole budget** (Settings → Advanced, on by default): use it or lose it. A wakeup keeps
  working until its budget is spent (the backlog, the free-time queue, new tasks it writes) instead
  of stopping when the planned work is done; busywork still doesn't count. If it stops early anyway,
  control tells it to carry on (twice by default; also in Advanced), and in Gate mode the reviewer
  must agree before it ends. Off: a wakeup's budget is planned and it may stop early with `IDLE:`.
- **Waiting for your reply**: when the assistant messages you or asks something during a run, the run
  stays open for your reply (5 minutes by default; Settings → Advanced → "Wait for your reply").
  Each reply resumes it and the window starts again, up to "Replies one run may wait for" (10). Time
  spent waiting doesn't count against the run's time limit.

What it can't do: change its own harness (control, the runner, its prompts, the containers),
approve its own requests, set secrets, or change settings and budgets. Changes to the harness are
done by you with a coding agent on the server; the assistant or the chat can draft the request.
