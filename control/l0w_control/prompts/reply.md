<!-- Control-owned. The Reply-now section of a brief. Archive old text before editing. -->

## Reply now: thread #{thread_id} “{title}” ({project})

The owner asked for a reply now. This run spends the reply budget ({budget} pts at most): read the
thread below and answer in it with `assistant send --thread {thread_id} "…"`. Keep it to a reply.
If the request needs real work, either ask to promote this run (`assistant promote --budget N
--reason "…"`, then `assistant wait {promote_wait}` for the decision; the request expires after {promote_expiry} minutes) or say so, claim the thread
(`assistant claim {thread_id} "plan"`) and plan a wakeup for it. The run is held open a few minutes
after you finish if you asked or messaged something, and resumes if the owner answers.

### The thread so far
