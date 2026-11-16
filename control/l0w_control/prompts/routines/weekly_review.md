<!-- Control-owned. The built-in weekly review routine. Archive old text before editing. -->
This is the **weekly review**, a few hours before the weekly usage reset.

1. Read the week: `journal/` since the last review, `NOW.md`, `plan/backlog.md`, `plan/roadmap.md`,
   the projects you touched (`projects/*.md`), and `assistant runs` / `assistant budget` for what the
   week cost.
2. Write `reviews/YYYY-Www.md`: what got done (with links), what didn't and why, what the owner
   asked for that's still open, what cost more than it was worth, and what you learned
   (move durable lessons into `knowledge/` and update `knowledge/INDEX.md`).
3. Re-plan: prune and reorder `plan/backlog.md` ("This week" for the coming week, each item with a
   "stop when"), update `plan/roadmap.md` if direction changed, and plan next week's wakeups
   (`assistant wake plan`) to the pace `assistant budget` suggests.
4. If something needs the owner (a decision, a stale project, a suggestion), file it as an ask or a
   suggestion rather than a long message. Send one short message to General only if the owner
   would want the summary.

If the sweep is on, this routine may run inside the sweep window: spend leftover budget on the
most valuable backlog item after the review, or end with `IDLE:` if nothing is worth it.
