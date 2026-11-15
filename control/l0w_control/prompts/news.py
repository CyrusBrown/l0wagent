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
]
