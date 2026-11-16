"""Harness news: dated, agent-facing changes, shown once in a wakeup brief and always
readable with `assistant news`. Control-owned like the prompts: add an entry whenever
something the agent uses changes; never edit an old one (add a correction instead)."""

NEWS: list[tuple[str, str, str]] = [
    ("2026-11-15", "Threads, asks, claims and suggestions", """\
You can now talk to the owner in threads (General, or one per project) and file asks:
- `assistant threads`, `assistant messages --thread N`, `assistant send --thread N "…"`, `assistant send --new "Title" [--project P] "…"`
- `assistant project list|show|new|update`
- `assistant question "…" --default X`, `assistant ask "…" [--allow-egress HOST] [--secret NAME] [--setting K=V]`, `assistant task "…"`,
  `assistant asks`, `assistant withdraw KIND ID`. Asks never block: file them and carry on.
- `assistant claim N "plan"` / `assistant claim done N "result"` when you pick up a concrete task from a thread.
- `assistant suggest KIND TARGET "why"` to propose archiving, renaming, moving, splitting or merging threads and projects; you can't change existing ones yourself.
- `assistant secret list|get NAME|env`, `assistant search "…"`, `assistant link thread N` (typed links: l0wagent://thread/N).
Answers, decisions and new messages reach you after your next tool call. If you message the owner or ask something,
your run is held open a few minutes and resumes if they reply."""),
    ("2026-11-15", "The shelf and the owner's uploads", """\
- Files the owner attaches land in `/work/_inbox/<project or general>/`. Messages and thread history
  (`assistant messages --thread N`, your Reply now brief) list them as `(attached: /work/…)`.
- `assistant shelf add PATH --project P [--label …] [--snapshot] [--keep N]` puts a deliverable on the project's
  Files tab. Live items (the default) always show the current file, so rerunning a script updates it;
  `--keep N` keeps N old versions. `--snapshot` freezes a copy. `assistant shelf list --project P`, `assistant shelf remove ID`.
- `assistant link file VOLUME/PATH` makes a typed link to a file for your messages."""),
    ("2026-11-16", "Budgets, your wakeup plan, routines, the reviewer and long jobs", """\
Nightly, review and turbo modes are gone. You now plan your own wakeups within budgets the owner sets:
- Budgets are in points (1 pt = 0.1% of a Claude subscription's week, or $0.10 on an API key). Every run
  has a budget and a deadline; control stops it at either. `assistant budget` shows what's left this week,
  your limits (allowed hours, wakeups per day, gap, points per wakeup, tiers), a suggested pace and your plan.
- `assistant wake at TIME --budget N --reason "…" [--tier T]` (the owner's local time), `assistant wake plan
  [--replace FILE]`, `assistant wake cancel ID`. Control checks each against the limits and says why if not.
  If nothing is planned for two nights, control adds a fallback wakeup. Your planning replaces it.
- Reply-now runs spend a small reply budget. For real work: `assistant promote --budget N --reason "…"`, then
  `assistant wait`. If that's denied or unanswered, claim the thread and plan a wakeup.
- Routines are scheduled prompts with their own tier and budget; the weekly review is built in. Propose one
  with `assistant routine propose …` (the owner approves).
- The reviewer (Monitor mode by default) reads your notes and may interject; talk to it with
  `assistant reviewer start|ask|done "…"`. In gate mode it must agree before you end with IDLE or close a
  claim as done. It can move you to another tier mid-wakeup.
- Register long background work: `assistant job start "label" --pid N --log /work/… --expect 2h`; end the job's
  command with `assistant job done ID; assistant notify "…"`. Never wait on it.
- Sweep wakeups near the weekly reset spend what the owner didn't use; end with IDLE if nothing is worth it.
- The owner can give standing instructions; they open every brief. Your core prompt was rewritten."""),
    ("2026-11-17", "Updates wait for you", """\
The owner now updates l0wagent with `./l0w update`, which waits until you're idle: your current run
finishes, and jobs started with `assistant job start … --dont-interrupt` are waited for (use it only for
work that can't survive a restart). While an update waits, no new run starts, so a wakeup can start late
or be marked missed; replan it if it mattered. Your volumes and plan survive the update."""),
    ("2026-11-17", "You may run on opencode", """\
The owner can now give you an opencode Go subscription instead of (or next to) Claude. Runs on it use
opencode as your agent CLI, with non-Claude models behind the same tier names (haiku, sonnet, opus)
that the owner picks. Everything else is the same: your `assistant` commands, your workspace and `CLAUDE.md`, budgets in points
(1 pt = $0.10 of the model's catalog price; free models cost nothing). Answers and the owner's messages
arrive appended to a tool's output, marked [control notification], instead of as a separate note."""),
]
