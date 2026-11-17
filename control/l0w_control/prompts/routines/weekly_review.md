<!-- Control-owned. The built-in weekly review routine. Archive old text before editing. -->
This is the **periodic review**, a few hours before the usage reset (the end of the week, or of the month on a monthly provider).

1. Read the period: `journal/` since the last review, `NOW.md`, `plan/backlog.md`, `plan/roadmap.md`,
   the projects you touched (`projects/*.md`), and `assistant runs` / `assistant budget` for what the
   period cost.
2. Write `reviews/YYYY-Www.md` (`YYYY-MM.md` on a monthly provider): what got done (with links), what didn't and why, what the owner
   asked for that's still open, what cost more than it was worth, and what you learned
   (move durable lessons into `knowledge/` and update `knowledge/INDEX.md`).
3. Re-plan: prune and reorder `plan/backlog.md` ("This period" for the coming one, each item with a
   "stop when"), update `plan/roadmap.md` if direction changed, and plan the next period's wakeups
   (`assistant wake plan`) to the pace `assistant budget` suggests.
4. If something needs the owner (a decision, a stale project, a suggestion), file it as an ask or a
   suggestion rather than a long message. Send one short message to General only if the owner
   would want the summary.

If the sweep is on, this routine may run inside the sweep window: spend leftover budget on the
most valuable backlog item after the review, or end with `IDLE: <reason>` as the last line if nothing is worth it.
