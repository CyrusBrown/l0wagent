# Wakeups across several providers (review of 2026-11-16)

How wakeups are planned, started, paid for and stopped today, why a second provider adds so little, and a design that fixes it in steps. Nothing in the code was changed. The design was accepted on 2026-11-16 (decisions at the end) and is summarised in the plan.

## Summary

Today the assistant has one working provider at a time. A second one (opencode Go next to a Claude subscription) can be the reviewer or the chat, but its allowance for wakeups goes unused unless the owner moves the worker role by hand. Underneath that are four structural causes:

1. **Everything hangs off "the worker's provider."** The agent's plan, the fallback, the sweep and `assistant budget` all read one provider.
2. **A point isn't the same amount of work on two providers**, and the code adds them together in places.
3. **A run is tied to one provider and one agent CLI**, so nothing can fail over or split work.
4. **Schedule limits live inside each provider's level**, although they're about the owner's day, not the provider.

The proposal, in the order I'd build it:

| Step | Change | What it buys |
|---|---|---|
| 1 | Schedule limits per instance; allowances per provider ("pools") | Two providers can both be planned against |
| 2 | Compare and add spend in list-price dollars; keep points for display | Correct sums; a budget can move between providers |
| 3 | A wakeup says what it needs; control picks the pool when it starts | Uses expiring quota first; a closed gate reroutes instead of skipping |
| 4 | Continue a run on another provider when one hits its limit | Runs survive a limit |
| 5 | Delegates: the main session hands sub-tasks to another provider | The strong model plans, the cheap one grinds |
| 6 | Wake on events and conditions, warn before stopping | Fewer wasted and cut-off wakeups |
| 7 | Model calls go through a proxy that holds the credentials | Exact metering on every provider; enforcement that can't be bypassed |

Steps 1 to 3 are the core. Each later step stands alone.

## What happens today

Read from the code at `56b5cf3`.

**Planning**
- `assistant wake at` and `wake plan` validate against `budgets.role("worker").provider` only (`api/agent.py:410`, `:441`). A wakeup can name a tier but not a provider.
- The fallback and the sweep run only for the worker's provider (`scheduler.py:186-188`). The sweep is hard-wired to the Claude subscription (`:192`, `:201`).
- `assistant budget` shows one provider. The brief's budget section shows the run's provider.
- Allowed hours, wakeups per day, the minimum gap and points per wakeup are fields of a provider's level (`budgets.py:85-94`). With two providers there are two sets, and only the worker's is used.

**Starting**
- `runs.start` takes the worker's provider (Reply now: the replier's), checks that one provider's gate, and refuses if it's closed (`runs/manager.py:249-254`). The scheduler then marks the wakeup skipped (`scheduler.py:276-280`). A subscription at its reserve skips the wakeup even when another provider has its whole allowance left.
- Only the subscription has a real gate (the usage meter). For key providers the gate is "a key is saved" plus the caps (`manager.py:225-229`).

**Running**
- One run at a time. A planned wakeup that can't start within 30 minutes of its time, because a long run is going, is marked missed (`scheduler.py:240-247`).
- A run's sessions all use the provider it started on. The reviewer can change the tier, by interrupting and resuming, but only within that provider's CLI.
- Mid-run usage checks exist only for the subscription (`manager.py:714`). What happens when opencode Go hits a plan limit mid-run is unknown (development.md says so).

**Paying**
- Each ledger row stores dollars at list price and points at its provider's rate: 1 pt is 0.1% of a Claude week, or $0.10 on a key (`budgets.py:462-469`).
- For the subscription, dollars become points through one number, `pts_per_usd`, learned from how far the weekly meter moved during runs. It starts at 2.0, takes a sample only when the meter moved a full 1%, and can't tell the assistant's use from the owner's during the same run (`budgets.py:471-479`).
- A run's total and the reviewer's share add up points across providers (`budgets.py:524`, `reviewer.py:79`): bug C4.

**Stopping**
- At 100% of its budget the run is interrupted (`manager.py:711-713`). The agent gets no notice at 80% and no turn to wrap up. Whatever wasn't checkpointed is lost, and the wakeup plan isn't updated, which is how the fallback ends up being needed.
- A session that ends without `IDLE:` ends the run, however much budget is left (bugs C1 and C2).

**Level controls**
- The level and Boost controls "act on the worker's provider" (development.md, M6). Every provider has its own level, so "Steady" on two providers means two schedules and 150 pts of two different sizes.

## 1. Split the schedule from the pools

Two kinds of limit are mixed in `Limits` today.

**The schedule, one per instance.** It describes when the assistant may be active and how often: allowed hours, wakeups per day, minimum gap, the longest a wakeup may run, whether there's a fallback. These protect the owner's sleep, the machine and the GPU. They don't depend on who is billed.

**Pools, one per provider.** Each provider has an allowance for its period, a reply budget, a reserve or caps, the models it offers, and whether unused allowance is lost at the reset.

**The level** becomes one instance-wide choice that sets the schedule and each pool's share. "Steady" might mean: nights only, two wakeups a day, 15% of the Claude week, $4 a week of the API key, 30% of the Go month. The owner can still override any one of them. Boost raises the instance level, as now.

Effects in the code: `Limits` splits into `Schedule` and `Pool`; `validate` checks the schedule once and then the pool; the per-day and gap queries drop their `provider=?` filter; the Calendar shows one plan with a pool label on each wakeup. The mockup's level picker already reads as instance-wide, so the UI moves toward it.

## One unit for work

A point is defined per provider, and that definition is the owner's (2026-11-15), so it stays as the unit people see. The trouble is only when two providers' points meet.

**Rule: store and compare dollars at list price; show points.** The ledger already has `cost_usd` on every row.
- A run's budget, the reviewer's share, a delegate's budget and "used so far" are computed in dollars. Each is shown in the points of the pool it's charged to.
- Moving a budget from one pool to another converts through dollars: 20 Claude points at a calibration of 2 pts/$ is $10 of work, which is 100 points on an API key. Then it's clamped to the new pool's room.
- A small `Cost` value type (dollars, provider) in `budgets.py` instead of bare floats makes adding two providers' points a type error.

List-price dollars aren't a perfect measure of work, since a cheap model does more per dollar. They are the one number every provider and every model reports, and they're what the owner pays on a key.

**Tell the agent what a point buys.** `assistant budget` should say, per pool, roughly how much work is left in plain terms ("about 6 hours of sonnet at your recent rate"), from the last two weeks of the ledger. The agent currently has to guess how big a 20-point wakeup is, and guesses differently per provider.

**Calibration for the subscription** is the weakest number in the system, and everything on the subscription is priced by it.
- Keep one value per model family. Opus and Sonnet count very differently against a plan's limits.
- Use the median of the last ten or so samples, not a running average that one odd run can drag.
- Discard a sample when the owner was clearly active: the chat ledger has rows in the interval, or the 5-hour meter moved much more than the run's own tokens explain.
- Start from the plan the owner says they have in the wizard (Pro, Max 5×, Max 20×) instead of 2.0 for everyone. The right value differs by an order of magnitude between them.
- For long runs, where the meter moved 2% or more, trust the meter over the estimate for the final charge.
- Show the calibration and its spread on the budget page. When it's uncertain, say so, and plan with the cautious end.

## 2. A wakeup says what it needs; control picks the pool

**Vocabulary.** Tiers are Claude's names today (`haiku`, `sonnet`, `opus`), mapped onto Go by list position (`GO_FAMILY_INDEX`). Make that mapping explicit and owner-editable: three **groups**, `light`, `standard` and `strong`, each naming a model and effort per provider. This is the smallest useful piece of M8's model registry (groups with fallback chains), pulled forward. Named models (`opencode-go/glm-5.3/high`) still work for a wakeup that wants one.

**Planning.** `assistant wake at TIME --needs standard --budget 20 --reason "…"`, optionally `--on claude` to pin a pool. Control validates the time against the schedule, and the budget against the pools that could run it: there must be room in at least one. It reserves against the pool the routing rule would pick now, and shows which.

**Routing, when the wakeup starts.** Among pools that offer the group, have room, and whose gate is open, pick in this order:
1. **The wakeup's pin**, if it has one and that pool can run it.
2. **Allowance that expires soonest.** For a pool whose unused allowance is lost at its reset: how much is left and unplanned, divided by the time to the reset. Spend the one with the most to lose first. This is the sweep's idea applied all period long, and it covers Go's monthly reset as well as Claude's weekly one.
3. **Flat-rate before metered.** A subscription costs nothing extra to use; a key costs money.
4. **The owner's order**, as the tie-breaker and as an override ("always prefer Claude for strong").

If the first choice's gate is closed, the next pool runs the wakeup with the budget converted. Only when no pool can run it is it skipped, and the note to the agent says why for each pool. Reply now routes the same way, so a subscription at its reserve no longer makes replies unavailable while another provider is idle.

The **sweep** stays as it is for the last hours before a subscription's reset, where the reserve is lifted. Rule 2 does the rest.

**A due wakeup while a run is going.** Today it's missed after 30 minutes. Tell the running session instead, through the same hook that delivers messages: "your 19:05 wakeup is due (race results posted): handle it now or wrap up." A timed wakeup exists because something happens at that time. The run that's already awake is the cheapest place to handle it.

## 3. Carrying a run across providers

Two different agent CLIs can't share a conversation. They can share everything else: the files, `NOW.md`, the run's notes and claims, the brief. That's enough for a **continuation**: a new session in the same run, possibly on another provider and CLI, started with a short prompt:

> Continuing run #214 after Claude reached its 5-hour limit. Your notes so far: … Open claims: … Read `NOW.md`, check the step you were in, and carry on. Budget left: 60 pts (opencode Go).

It works as well as the agent's checkpoints are recent, which the manual already asks for and the reviewer already checks.

**When a provider hits its limit mid-run**
- Detect it: Claude Code reports rate-limit events (control already stores them); for opencode, the error text. This needs one real test per provider; it's on the "unverified" list.
- If the limit lifts soon (a 5-hour window resetting within the run's deadline and within, say, 20 minutes): pause without spending and resume the same conversation. `assistant wait` already works this way.
- Otherwise: continue on the next pool, by the routing rule.
- Record what was learned. Go has no usage meter, so the moment it refuses is the best information there is about its limits: mark the pool exhausted until its reset and tell the owner.

**The agent asking for it.** `assistant continue --needs strong --reason "the refactor is harder than planned"` ends the session and continues on another group, across providers if needed. Within one provider the existing tier switch stays, since it keeps the conversation.

## 4. Delegates: the strong model plans, the cheap one works

This is where a second provider pays for itself. A scarce, strong allowance (a Claude subscription) and a cheap, flat-rate one (opencode Go) are best used together in one run, not on alternate nights.

`assistant delegate "task" --needs light --budget 3 [--dir /work/x] [--read-only]` starts a child session:
- on the pool the routing rule picks for that group, which may be another provider and CLI;
- with the task, the folder it may work in, and nothing else: no agent token, no secrets, no brief. It returns its final message, and its file changes stay in the folder;
- charged to its own pool, counted in dollars against the parent run's budget, shown as its own row in the run's timeline;
- up to two or three at once, under the run's lease.

The runner's `/consult` (a second session with no hook, secrets or agent token, built for the reviewer) is most of the machinery. A delegate is a consult that may write inside one folder.

Good uses: reading and summarising a large codebase or a pile of documents, run-fix-rerun loops with a clear target, boilerplate and tests from a spec, bulk renames. The parent decides and checks. The core prompt needs a short section on when to delegate and how to write a task that needs no context.

The reverse pattern already exists: a cheap worker asking a strong reviewer (`assistant reviewer ask`). Advisor mode is that. It's worth saying in the prompt, as the cheap way to get a strong opinion.

## 5. Fewer wasted wakeups

**Wake on an event, not on a guess.** The agent registers a job expected to take two hours and plans a wakeup two hours out. If the job takes forty minutes or four hours, the wakeup is late or empty.
- `assistant job start … --wake-when-done --budget 5`: control starts a small wakeup when the job's process ends.
- `assistant question … --wake-on-answer --budget 3`: the owner's answer starts one. Today an answer waits for the next planned wakeup, which can be a day.
- A file arriving in `_inbox/`, and later a webhook through a brokered integration.

These spend the allowance, obey the schedule (or wait for its next allowed hour), count toward the daily limit, and are capped per day so a flapping event can't drain the pool.

**Check before waking.** `assistant wake at … --if tools/race-posted.sh`: at the time, the runner executes the agent's script in the sandbox, with no model. Exit 0 starts the wakeup; anything else skips it and can retry every N minutes until a deadline. "Have the results been posted", "did the backup finish", "is there a new release" cost nothing to check this way.

**Warn before stopping.** Send two notices through the hook: at 75% of the budget ("wrap up within the next few steps: checkpoint, close claims, update your plan") and at 90%. Do the same ten minutes before the deadline. The hard stop stays where it is. This alone should cut how often the fallback has to rescue a plan.

**Don't end early by accident.** When a session ends cleanly with more than a quarter of its budget left and no `IDLE:` line, resume it once with "you have 31 pts left: carry on, or end with `IDLE:` and the reason." See bug C2.

**Know what resuming costs.** Every resume (after holding for the owner's reply, after a tier change, after a wait) re-reads the whole conversation, and after about five minutes the provider's prompt cache has expired, so it's re-read at full price. `HOLD_MINUTES` is 5, right at that edge. Show each resume's cost in the timeline, and consider holding for 4 minutes, or holding longer only when the conversation is short. Confirm the cache lifetime per provider before tuning.

**The subscription's 5-hour window.** A window opens with the first use after the last one closed. A wakeup at 02:00 opens a window that's gone by 07:00; the same wakeup at 06:30 opens one the owner shares until 11:30. The reserve only checks the meter at start (70%). Add an owner setting "keep my window clear from HH:MM", have control refuse or shrink wakeups whose window would run into it, and show the current window's reset in `assistant budget`. Check with a real account how the window anchors before building this.

## Credentials stay in control

The largest change here, and the one that makes the rest exact.

Today the agent container holds the provider credentials: the Claude login in its home folder, keys in its environment. Control meters by reading the CLI's event stream and enforces by interrupting the process. Consequences:
- The egress lease is the only thing limiting use outside runs, and it can be bypassed (bug S1).
- The agent could copy a credential out.
- Metering depends on each CLI's output format. opencode Go has no meter at all.
- Two programs refreshing one Claude login can invalidate each other. agents-and-runs.md already flags this.

**Proposal: a model proxy.** The CLIs are pointed at a base URL inside the stack (in egress, or a small service next to it). The proxy adds the credential, forwards over TLS to the provider, and reads the token counts from each response.
- No provider credential in the sandbox. The lease question disappears: without a run, the proxy refuses.
- Exact usage per request, for every provider and every model, including delegates and the reviewer, attributed by a per-session token control issues.
- Enforcement by refusing the next request at the budget, with a clear error the model reads, instead of an interrupt.
- Rate-limit headers and errors are seen directly, which gives step 3 its signal and gives Go a meter of sorts.
- Routing and failover can eventually happen per request, below the CLI.

**What has to be checked first** (a Phase-0 test, like the one done for opencode): that Claude Code works against a custom base URL when the account is a subscription, how the login's refresh is handled when control owns it, and whether Anthropic's and opencode's terms are comfortable with a local pass-through in front of their own client. If the subscription can't go through a proxy, do it for key providers and keep the lease, with the S1 fix, for the subscription.

## What the agent would see

```
$ assistant budget
Schedule (Steady): 22:00–08:00, at most 2 wakeups a day, 2 h apart, 3 h each.

Pools
  claude   110 of 150 pts left · resets Sat 09:00 (2.4 days) · 40 planned
           ≈ 5 h of standard at your recent rate · light, standard, strong
           5-hour window: 12% used, resets 04:10
  go       310 of 380 pts left · resets Dec 1 (14 days) · 0 planned
           ≈ 30 h of standard · light, standard · no more than 76 pts in any 5 h
  api      $3.10 of $5.00 left this week · used last

Next to expire: claude (70 pts unplanned, 2.4 days). New wakeups go there first.
Reply budget: 24 of 30 pts (claude), 40 of 40 (go).

Plan
  Tue 02:00  standard  20 pts  claude   "finish the importer tests"
  Wed 19:05  light      5 pts  go       "race results posted at 19:00"   if tools/race-posted.sh
  when job #12 ends     5 pts           "check the training run"
```

## Order of work

As first proposed. The accepted order, with the proxy moved earlier, is the Providers track in [development.md](../plans/development.md#providers-track).

1. **Dollars for arithmetic** (the `Cost` type, bug C4). Small, no behaviour change, removes a class of error.
2. **Warnings at 75% and 90%, and the early-end nudge.** Small, and helps single-provider instances today.
3. **Schedule and pools**, with the instance-wide level. The biggest refactor; mostly `budgets.py`, the scheduler's queries and the budget settings screen.
4. **Groups and routing**, including Reply now. Then `assistant budget` shows all pools.
5. **Calibration** per family, seeded from the plan size.
6. **Continuations and limit handling.** Needs the real-account tests.
7. **Event and conditional wakeups.**
8. **Delegates.**
9. **The model proxy**, after its Phase-0 test. If the test passes early, do it before step 6: it makes 6 and 8 simpler.

Each step needs a harness-news entry and, from step 3, a rewrite of the budget section of `core.md` and the brief.

## Decisions (2026-11-16)

The owner left these to me. They're now in [agents-and-runs.md](../plans/agents-and-runs.md#several-providers-planned), and the order of work is the Providers track in [development.md](../plans/development.md#providers-track).

| # | Decision | Why |
|---|---|---|
| 1 | **One instance-wide level, with a share per pool.** | The schedule is about the owner's day; one choice should set it |
| 2 | **Routing: the pin, then expiring allowance, then flat-rate before pay-per-use, with the owner's order as tie-breaker and override.** | Uses what would be lost first, and costs nothing extra by default |
| 3 | **A pay-per-use pool takes over from another pool only when the owner turns that on.** Off by default. | It spends real money; that's the owner's call each time or once |
| 4 | **Delegates are on at every level above Dormant**, inside the run's budget, with an owner switch. | They add no spend, and they're the main gain from a second provider |
| 5 | **The model proxy gets its Phase-0 test first** (step P0), and is built before continuations if it passes. | It makes metering exact and removes credentials from the sandbox; continuations and delegates are simpler on top of it |
| 6 | **A run that ends early without `IDLE:` is resumed once.** | The plan says the agent works until stopped; today it just ends |
