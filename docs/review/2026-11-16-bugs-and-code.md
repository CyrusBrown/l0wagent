# Bugs and code improvements (review of 2026-11-16)

Found by reading the code. **Status (2026-11-17): every item below is fixed, with a regression test where practical**, except:
- **S23** got only the short-term fix (runner answers only control; a model CLI started outside the session is flagged on the timeline). The real fix is still the model proxy.
- **S24**: the reviewer runs as a separate `reviewer` user with a scrubbed environment; still to confirm with a real consult that Claude Code logs in from the copied credentials file.
- **Code improvement 11**: the read-connection pool is in; moving the run follower's database calls off the event loop is not (about 80 calls across `runs/manager.py`).
- **Code improvement 14**: no `AGENTS.md` seeded. opencode falls back to `CLAUDE.md` when there's none and finds `.claude/skills/`; confirm with the real-model smoke suite (`dev/smoke/`).
- **Code improvement 9**: `dev/smoke/smoke.py` exists and passes `--dry-run` on the fake stack; not yet run on a real account.

The original text follows. The baseline was clean when I read it: 156 control tests pass and ruff is clean.

Each item says what is wrong, where, and how to fix it. Line numbers are from commit `56b5cf3`.

Order within each section is by how much it matters. Items marked **(before the hub)** are tolerable on a LAN and become serious once the instance has a public URL.

## Security

### S1. The egress lease can be bypassed with an IP address **(binding invariant)**
- **Where:** `egress/egress.py:99` (`is_gated`), `:176` (`decide`).
- **What:** the gate checks the hostname in the `CONNECT` line only. The proxy doesn't decrypt TLS, so `CONNECT <an Anthropic IP>:443` with `api.anthropic.com` as the TLS server name passes as an ordinary public address, with no lease. `curl --resolve api.anthropic.com:443:<ip>` is enough. The same trick gets past any owner block rule written as a domain.
- **Fix:** after answering `200 Connection Established`, read the client's first TLS record (the ClientHello, up to 16 KB, a few seconds' timeout) and take the server name from it. Apply `is_gated` and the block rules to that name as well as the `CONNECT` host, and close the tunnel if it's gated without a lease. Start the upstream-to-client pipe first so protocols where the server speaks first (SSH, SMTP) don't stall. Register the tunnel under the server name so `close_gated_tunnels` closes it when the lease ends. Add a test that feeds a recorded ClientHello to the parser.
- **What this doesn't solve:** with `DEFAULT_POLICY=allow` the agent can still relay through any third-party host it can reach. The lease is a guard against accidental use, not against a determined agent. The durable fix is in [the wakeups review](2026-11-16-wakeups-and-providers.md#credentials-stay-in-control): keep provider credentials out of the sandbox.

### S2. `/api/agent/*` accepts the agent token from any address **(before the hub)**
- **Where:** `control/l0w_control/guard.py:60`.
- **What:** the guard stops the sandbox calling anything else, but it doesn't stop anything else calling the agent API. The token lives in the untrusted container, so the agent (or anything that tricks it) can hand it to an outside party, who can then read every thread and every agent-scope secret (`GET /api/agent/secrets/NAME`) from the LAN, and from the internet once a tunnel exists.
- **Fix:** refuse `/api/agent/*` unless the request comes from the sandbox subnet. Keep an explicit `L0W_AGENT_API_ANY_ADDRESS=1` for `dev_stack.py`, where the runner is on localhost. Same rule for `/api/chat-runtime/*` when no chat subnet is set.

### S3. The session cookie is never marked Secure
- **Where:** `control/l0w_control/api/routes.py:49`; `Env.cookie_secure` (`config.py:38`) is read nowhere.
- **Fix:** pass the request into `_set_session` and set `secure=True` when `cookie_secure` is `always`, or `auto` and the request arrived over HTTPS (the scheme, or `X-Forwarded-Proto` from a trusted proxy, see S13). Rename the cookie to `__Host-l0w_session` when secure: that stops a sibling hostname setting it, which matters on a shared hub domain ([hub review](2026-11-16-hub.md#2-one-domain-many-members)).

### S4. Tool results reach the sidecar and outside chat services unredacted
- **Where:** `control/l0w_control/chat.py:649-654` (`mcp`), `connector.py:272` (`call`).
- **What:** the in-control chat loop is safe by accident: it stores the redacted result and rebuilds the history from the database. The sidecar path and the connector return `T.run`'s raw text. `read_file` on a `.env` the agent wrote under `/work` hands vault values to Claude Code in the sidecar or to ChatGPT.
- **Fix:** redact once in `tools.run` (`ctx.be.vault.redact(text)`) so every caller gets the same thing. Test: save a secret, write it to a workspace file, call `read_file` through `/mcp`.

### S5. Saving a secret doesn't scrub chats or several other tables
- **Where:** `control/l0w_control/vault.py:30` (`SCRUB`).
- **What:** the chat prompt tells the owner a pasted secret's stored copy is redacted. That's only true for tables on the list, which stops at M2's.
- **Fix:** add `chat_messages(body, data_json, raw_json)`, `chats(title)`, `reviewer_messages(body)`, `integration_calls(args_json, detail)`, `wakeups(reason)`, `jobs(label, progress, note)`, `projects(name, summary)`, `threads(title)`, `notifications(title, body)`. Add a test that walks the schema and fails when a `TEXT` column that holds owner or agent text is missing from the list, so the list can't fall behind again.

### S6. Notes, status and run descriptions are stored unredacted
- **Where:** `control/l0w_control/api/agent.py:65`, `:75`, `:86`.
- **What:** the timeline copy is redacted; `run_notes.text`, `runs.now_sentence` and `runs.description` aren't. Push notifications and Home read those columns.
- **Fix:** redact before the three writes.

### S7. "Allow once" given after the asking run ended is never undone
- **Where:** `control/l0w_control/conv.py:514-519` (`revert_once`).
- **What:** it reverts actions whose permission belongs to this run, or that were applied after this run started. An approval given between runs matches neither, so a "once" egress rule, setting or secret stays for good, while the agent is told it was temporary.
- **Fix:** at the end of a run, revert every applied, unreverted `once` action (only one run exists at a time). Change `SCOPE_ONCE_NOTE` to "undone when the run that asked ends, or the next run if it already has". Test: file in run A, approve after A ends, check it's reverted at the end of run B.

### S8. The agent may propose a change to any setting
- **Where:** `control/l0w_control/conv.py:868-876` (`_check_action`), `config.py:49` (`SPECS`).
- **What:** a permission card can carry `set USAGE_UNKNOWN_POLICY to allow` or `set LOGIN_LOCKOUT_FAILURES to 100000`. The owner has to click, but the card's own text is the agent's, and values aren't range-checked.
- **Fix:** give each spec a fourth field: who may propose it (`agent` or `owner`) and a min/max. Only run-shape settings (`HOLD_MINUTES`, `REPLY_MAX_MINUTES`, `WAKEUP_MAX_MINUTES`…) are agent-proposable. Never the usage policy, the reserves, or anything under `LOGIN_`/`SESSION_`. Enforce the range in `Settings.set`.

### S9. A new secret is announced to the agent whatever its scope
- **Where:** `control/l0w_control/conv.py:795-798`.
- **What:** the GitHub preset saves `GITHUB_TOKEN` as "Integrations only", then the agent is told it's "in your environment from the next session", which contradicts the harness news ("no token is in your environment; don't look for one").
- **Fix:** `integrations` scope: mark announced, say nothing. `project:`/`routine:` scope: announce with the limit ("only in runs on project X's threads").

### S10. An egress rule of `.` breaks the proxy
- **Where:** `egress/egress.py:103-117` (`parse_pattern`), `:130-135` (`_match`); the agent-side regex at `conv.py:859` accepts it.
- **What:** `.` parses to an empty domain with no network, and `_match` then evaluates `ip in None`. Every request fails until the rule is deleted.
- **Fix:** reject an empty domain after stripping dots, in both places; require at least one alphanumeric label. Make `_match` return False when a rule has neither a domain nor a network.

### S11. Anyone can use up the connector's client registrations **(before the hub)**
- **Where:** `control/l0w_control/connector.py:100`.
- **What:** registration is open (as the MCP spec asks) with a hard cap of 100, never pruned. The error tells the owner to revoke apps in Connected apps, but that page lists grants, so unconsented clients can't be seen or removed.
- **Fix:** on each registration delete clients older than a day that have no grant, and at the cap drop the oldest grantless ones. Rate-limit by address.

### S12. A reused refresh token doesn't revoke the grant
- **Where:** `control/l0w_control/connector.py:187-194`, `:197-204`.
- **What:** tokens rotate, but presenting an already-rotated one only fails. OAuth 2.1 asks public clients' servers to treat that as theft and revoke the grant, as the code already does for a replayed authorization code.
- **Fix:** look the token up including revoked rows; if it was revoked by rotation, call `_revoke_grant`. Allow a few seconds' grace for a client that retries a refresh whose response it lost.

### S13. Control trusts the socket address and `X-Forwarded-Proto` unconditionally **(before the hub)**
- **Where:** `control/l0w_control/api/routes.py:53` (`_client`), `:779`.
- **What:** behind a tunnel or reverse proxy every request comes from one address. The per-address login limit becomes a global one (ten bad passwords from anyone lock out everyone), and new-device alerts show the proxy.
- **Fix:** `L0W_TRUSTED_PROXIES` (addresses or subnets). Only from those, take the client from `CF-Connecting-IP` or the last untrusted hop of `X-Forwarded-For`, and the scheme from `X-Forwarded-Proto`. One helper used by `_client`, `_origin`, S3 and the guard.

### S14. Account lockout lets a stranger keep the owner out **(before the hub)**
- **Where:** `control/l0w_control/auth.py:158-169`.
- **What:** five wrong passwords for the account from anywhere lock all new logins for fifteen minutes, repeatable forever. Existing sessions keep working, so it bites when the owner needs a new device.
- **Fix:** drop the hard account lockout. Keep the per-address limit, add a global slow-down (one password check every couple of seconds once failures pile up), and exempt addresses that have logged in successfully before. With 2FA on, a wrong password costs an attacker nothing useful anyway.

### S15. TOTP: plaintext secret, no recovery codes, client-chosen secret
- **Where:** `control/l0w_control/auth.py:303-319`.
- **Fix:** encrypt `totp_secret` with the vault key. Keep the pending secret server-side between `totp_setup` and `totp_enable` instead of trusting the one the client sends back. Issue ten one-time recovery codes (hashed) when 2FA is turned on, and document `./l0w` as the reset path for someone with shell access.

### S16. Brokered integrations trust the server's own "read-only" label
- **Where:** `control/l0w_control/integrations.py:266-271`.
- **What:** a tool the upstream marks `readOnlyHint` starts allowed. A compromised or dishonest server labels a write tool read-only and it runs without a card. A tool whose description or schema changes after approval keeps its policy.
- **Fix:** start every tool of a non-preset integration on `ask`; let presets carry their own vetted read-only list. Store a hash of each tool's description and schema, and when it changes set the tool back to `ask` and show "changed since you allowed it".

### S17. Control will call any URL an integration names
- **Where:** `control/l0w_control/integrations.py:375` (`_check_url`), `:59-67`.
- **What:** control sits on the `ctl` and `sandbox` networks and isn't behind egress. An integration URL of `http://egress-admin:8090/…` or `http://agent:8765/…` makes the trusted side call internal services, with a bearer token attached. The owner approves the card, but a chat model wrote it (`propose_integration`).
- **Fix:** resolve the host and refuse loopback, link-local and the stack's own networks; require https unless the address is private and the owner ticks "this is on my network"; connect to the vetted address. The hub's gateway needs the same check ([hub review](2026-11-16-hub.md#the-gateway)), so write it once.

### S18. Nothing limits how many asks or notifications a run can raise
- **Where:** `control/l0w_control/api/agent.py:246-279`; `integrations.py:326` (each distinct argument set of an "ask" tool files a new card).
- **What:** a looping or manipulated agent can push hundreds of "Approval needed" notifications. That's an annoyance now and a way to wear the owner down into approving later.
- **Fix:** caps per run and per hour (say 20 open asks, 10 notifications an hour), after which asks are still filed but notifications are folded into one "12 more waiting". Return a clear error to the agent at the hard cap.

### S19. URLs from the agent and from chat models aren't scheme-checked
- **Where:** `control/l0w_control/api/agent.py:178` and `:191-194` (project links; the patch route stores the dicts as sent), `tools.py:221`, `:245` (`help_url`, `token_url`); rendered at `web/src/screens/Projects.tsx:118`, `Chat.tsx:264`, `:275`.
- **What:** React 19 refuses `javascript:` links, so today's web app is safe. Any other client (the desktop app's webview, a future one) isn't.
- **Fix:** one `safe_url()` on the server that accepts only `http`, `https` and `l0wagent`, used for every stored link. Give `ProjectPatchIn.links` the same shaping as create.

### S20. Messages posted by a chat or a connected app reach the agent as plain "Owner"
- **Where:** `control/l0w_control/conv.py:768`; `thread_history` (`:820`) already says "Owner (via chat)".
- **What:** an outside model with the write scope can be steered by whatever it read. The agent should know the message came through one.
- **Fix:** include the message's `via` in the delivery line: "Owner, via ChatGPT (connector)".

### S21. Read-only egress rules forward method-override headers
- **Where:** `egress/egress.py:345`.
- **What:** some LAN apps honour `X-HTTP-Method-Override: DELETE` on a GET.
- **Fix:** for `allow_ro`, drop `X-HTTP-Method-Override`, `X-Method-Override` and `X-HTTP-Method`.

### S22. The release build lets any page on `localhost:5173` act as the owner
- **Where:** `control/l0w_control/app.py:27` (`DEFAULT_ORIGINS`), `:61`.
- **What:** the Vite dev server's origin is in the default CORS list, with credentials and every header allowed, in the release image too. When the owner reaches control as `localhost` (Docker Desktop on a Mac, the common case for a developer), `localhost:5173` is the same site, so the session cookie is sent, and CORS lets that page add the `X-L0W-Client` header and read the answers. Any dev server the owner happens to run on that port, or any page a dependency serves there, has the whole owner API, approvals included.
- **Fix:** the default list is the two Tauri origins only. `compose.dev.yaml` and `dev/ui-demo.sh` set `L0W_CORS_ORIGINS` for Vite.

### S23. Metering and budgets rely on the sandbox cooperating
- **Where:** by design: the provider login and keys are in the agent container (`agent/runner/runner.py:305`), and control meters by reading the session's event stream.
- **What:** during any run the lease is open and the credentials are local, so `claude -p …` or `opencode run …` from a tool call is a second model session control never sees. A model can do this in good faith ("I'll delegate this"). On the subscription the weekly meter still moves, which skews the calibration; on an API key or opencode Go nothing notices until the bill. The reported `total_cost_usd` is also whatever the container says it is. Separately, `RUNNER_TOKEN` is removed from the session's environment but any process there can read it from the runner's `/proc/<pid>/environ`.
- **Fix, short term:** during a run, have the runner list model-CLI processes that aren't its own session (it already answers `/procs`) and put a warning on the timeline; say in `core.md` that starting the model CLI directly isn't allowed and why. Have the runner accept requests only from control's address, since the token can't be hidden from a root user in the same container.
- **Fix, real:** the model proxy in the [wakeups review](2026-11-16-wakeups-and-providers.md#credentials-stay-in-control). With S1 this is the strongest argument for it.

### S24. The reviewer's "read-only" is a request, not a restriction
- **Where:** `agent/runner/runner.py:588-634`.
- **What:** the docstring and server-platform.md say the reviewer has no agent token, no secrets and no file-writing tools. It runs as the same user in the same container with Bash allowed: it can write files through the shell and read the agent token from another process's environment. It reads untrusted workspace content all day.
- **Fix:** a second unix user in the image with no sudo, read access to `/workspace` and `/work`, and no access to the agent's home; run consults as that user with a scrubbed environment. Until then, say "asked not to write" in the docs. Delegates (wakeups review, step 5) need the same mechanism, so build it once.

### S25. The offline outbox silently throws away what the server refuses
- **Where:** `web/src/api/offline.ts` (`flushOutbox`, the `res.status >= 500` check).
- **What:** any answer below 500 removes the item. A session that expired while the phone was offline answers 401, so every queued message, answer and finished task is deleted without a word. 429 does the same. The `error` field on outbox items is never set.
- **Fix:** on 401 stop and keep the queue until the owner signs in again. On 429 stop and retry later. On other 4xx keep the item, set its error, and show it in the outbox with Retry and Discard. Clear the read cache on 401 as well: a session revoked from another device ("lost phone") currently leaves a week of threads readable on the lost one.

### S26. ntfy carries message previews in the clear
- **Where:** `control/l0w_control/notify.py:126-131`.
- **What:** the body (up to 300 characters of the assistant's message or an approval's text) is posted to the topic URL. On the public ntfy server a topic is readable by anyone who knows or guesses its name, and there's no way to send an access token.
- **Fix:** an optional token (sent as a bearer header), a default of "title only" for ntfy with previews opt-in, and a line in Settings saying a public topic is public. Vet the URL like S17.

### S27. Zips and raw reads hold whole files in memory
- **Where:** `control/l0w_control/files.py:204` (`zip` calls `read`), `:149`.
- **What:** zipping a folder with a 1 GB file reads it into control's memory, then compresses it there. The agent decides how big its files are; the owner only clicks Download.
- **Fix:** write into the archive in chunks (`ZipFile.open(name, "w")` with a loop over `os.read`), and use `iter_file` for every raw download.

### S28. Git history is opened by path after the symlink check
- **Where:** `control/l0w_control/files.py:215-246`.
- **What:** `_real_git` checks that `.git`, `objects` and `refs` are real folders, then dulwich opens the repository by path and follows whatever is inside: a symlinked `HEAD` or ref, `objects/info/alternates`, `commondir`. Nothing returns the content of such a file today (invalid refs and objects raise), so this is hardening, not a leak.
- **Fix:** before opening, refuse a repo where `HEAD`, `packed-refs`, `config`, `commondir`, `objects/info/alternates` or anything under `refs/` is a symlink, and refuse `alternates` and `commondir` outright. Add a test with each.

## Correctness

### C1. `IDLE:` is only recognised at the start of the final message
- **Where:** `control/l0w_control/runs/manager.py:531`, `:605`, `:611` (three checks, two of which treat leading whitespace differently).
- **What:** the prompt says "end with a line starting `IDLE:`". A model that writes a short wrap-up and then the `IDLE:` line isn't seen as idle, so a sweep keeps scheduling more runs and gate mode never asks the reviewer.
- **Fix:** one `ends_idle(text)`: true when the first or the last non-empty line starts with `IDLE:`. Use it in all three places and in the fake runtime's tests. Tighten the prompt to "make `IDLE: <reason>` the last line of your final message".

### C2. Gate mode is skipped when a run ends without `IDLE:`
- **Where:** `control/l0w_control/runs/manager.py:531`.
- **What:** `_gate` returns early unless the final text starts with `IDLE`. Models usually just stop. Then the run ends with nobody having agreed.
- **Fix:** in gate mode, send every clean end to the reviewer, with "(it didn't write IDLE)" in the prompt. Related design gap: outside gate mode, a run that stops early with most of its budget left simply ends. The plan says "the agent works until stopped, or ends with `IDLE:`". Either nudge once ("you have 31 pts left; carry on or end with IDLE and why") or change the plan's wording.

### C3. One bad routine stops every wakeup
- **Where:** `control/l0w_control/scheduler.py:82-87`.
- **What:** `tick` runs its steps in sequence; an exception in `routines_to_plan`, `fallback` or `sweep` skips `start_due`, every 15 seconds, forever.
- **Fix:** run each step in its own try/except with a log line, as the housekeeping jobs already are. Surface a repeated failure on Settings → System.

### C4. The reviewer's spend is added up in the wrong unit when it runs on another provider
- **Where:** `control/l0w_control/reviewer.py:79`, `:83-85`; `budgets.py:524` (`run_pts`).
- **What:** ledger rows carry points at their own provider's rate (0.1% of a Claude week, or $0.10). A Claude worker with a reviewer on opencode Go sums the two as if they were the same thing, for both the reviewer's share and the run's total.
- **Fix:** compare in dollars (`cost_usd`), and charge the reviewer's rows to its own provider's allowance. See [one unit for work](2026-11-16-wakeups-and-providers.md#one-unit-for-work).

### C5. Monthly periods: "week" is still hard-coded in places
- **Where:** `scheduler.py:133` (a `before_reset` routine's next time is `+7 days`), `:267`, `:269`; `chat.py:701`; `runs/manager.py:308` ("the weekly allowance"); the weekly review routine is created on the subscription only (`routines.py:74`).
- **Fix:** compute the next reset from `budgets.week(provider, end + 1 s)`; use `period_unit` in every message; let the review routine follow the worker's provider.

### C6. Idempotency keys aren't atomic, and several tables grow without limit
- **Where:** `control/l0w_control/conv.py:77-89`; no code deletes from `idempotency_keys`, `login_attempts`, `run_events`, `timeline_items`, `integration_calls`, `oauth_codes`, `oauth_tokens`.
- **Fix:** insert the key first inside the transaction (`INSERT … ON CONFLICT DO NOTHING`, then read), so two retries can't both run `make`. Add a daily retention job: keys 7 days, login attempts 30 days, expired OAuth rows at once, raw `run_events` after a configurable number of days (keep the normalised timeline).

### C7. The fallback wakeup comes back, with a new note, every time the agent plans
- **Where:** `control/l0w_control/scheduler.py:156-174`, `:297-302`.
- **What:** planning any wakeup cancels the fallback. If that wakeup is more than 48 hours out, the next tick plans another fallback and writes another note.
- **Fix:** don't cancel the fallback when the new plan leaves the next 48 hours empty; write the note only the first time in a period.

### C8. The brief's "Waiting on the owner" list has no content
- **Where:** `control/l0w_control/runs/manager.py` (`_prompt`, the `waiting` block).
- **What:** each line is "question in thread #4 …" with neither the id nor the text, so the agent has to look each one up.
- **Fix:** `- question #12 in thread #4 “Title”: <first 100 characters>, asked 3 days ago`. Age matters: the agent should withdraw or re-ask stale ones.

### C9. Search treats `%` and `_` as wildcards and scans whole tables
- **Where:** `control/l0w_control/conv.py:829-850`.
- **Fix:** an FTS5 index over message bodies, thread titles and run descriptions, kept current by triggers; escape the query for the remaining `LIKE`s.

### C10. The hook waits three seconds per tool call when control is slow
- **Where:** `agent/bin/assistant-hook` (a 3-second timeout, on every tool call).
- **What:** runs are meant to carry on while control restarts. If control is hung rather than gone, each tool call stalls for the full timeout, and a run with hundreds of tool calls crawls.
- **Fix:** a small state file: after a failure, skip the check for 30 seconds.

### C11. Two prompts name commands that don't exist
- **Where:** `control/l0w_control/prompts/routines/weekly_review.md:5` (`assistant runs`), `prompts/resetup.md:17` (`assistant help`); `agent/bin/assistant` has neither.
- **What:** the weekly review tells the agent to read what the week cost with `assistant runs`, and gets an argparse error. The migration brief sends it to `assistant help`.
- **Fix:** add both. `assistant runs [-n 20]` lists recent runs (id, when, why, used of budget, how it ended, its description), and `assistant run show N` gives one run's notes, claims and result. This is the "per-run usage" record the plan says control keeps so the agent doesn't rebuild it; today the agent can't read it. `assistant help [topic]` prints the usage text, by section.

### C12. The brief doesn't say how the last run ended
- **Where:** `control/l0w_control/runs/manager.py` (`_prompt`).
- **What:** if the previous run was cut off at its budget or deadline, failed, or was stopped by the owner, the next brief doesn't mention it. The agent learns it only if `NOW.md` was written in time, which is exactly what a cut-off run didn't do. Control knows precisely what happened.
- **Fix:** a "Since your last run" section: run #N, when, how it ended and why, its last status sentence and last three notes, claims it left open, files it changed without committing (the safety-net commit's list). A few lines, written by control from its own records.

## Prompt wording (noted, not rewritten)

The prompts read well. These are the places where the text and the code disagree, or where the agent is left guessing.
- **`core.md`**: `assistant wake plan --replace` needs a file argument and the format isn't described anywhere in the prompt. `assistant wait` isn't mentioned outside the Reply-now section. There's no rule about secrets in messages ("never paste a secret's value into a thread, a note or a file outside `/work`"). "This week" is wrong on a monthly provider. `IDLE:` (C1).
- **`brief.md`**: "the weekly allowance"; the deadline is printed as a raw UTC timestamp while every other time is the owner's local time.
- **`reply.md`**: tells the agent to `assistant wait` after `promote` but not for how long (`PROMOTE_WAIT_MINUTES`).
- **`reviewer.md`**: "each of your turns spends the owner's usage on a strong model" is false when the reviewer is a cheap one; fill it from the config. It isn't told which provider the worker is on or that a tier change can't cross runtimes.
- **`chat.md`**: "(the stored copy is redacted)" is only true after S5. "Mention the cost" of Reply now gives the model no number; put the reply budget left into the system prompt next to the time.
- **`weekly_review.md`**: written for a weekly reset; on a monthly provider the first line is wrong (C5).
- **News, 2026-11-18 "Brokered integrations"** and the delivery text in S9 contradict each other until S9 is fixed.
- **Missing altogether**: guidance on what a good message to the owner looks like (they often read on a phone: the answer first, short, links instead of pasted output), and on what to do when a provider limit is hit mid-run.

## Code improvements

These aren't bugs. They're the changes that would make the next milestones cheaper.

1. **Split the guard's rules from addresses.** `guard.py` decides by path prefix and source subnet in one function. The hub adds a third and fourth kind of caller. Turn it into a small table: caller class (sandbox, chat runtime, tunnel, LAN) → the path prefixes it may reach, default deny. S2 and the hub's public-surface rule both fall out of it, and it's testable as data.
2. **Test the egress proxy directly.** It's the main enforcement point and has one test file, for the chat subnet. Add table tests for `decide` (private addresses, rules, the lease, rebinding) and for the new ClientHello parser.
3. **A schema check for redaction and scrubbing** (see S5), and one helper, `store_text()`, that every write of agent or owner text goes through, instead of remembering to call `vault.redact` at each site (S6 is what forgetting looks like).
4. **Redaction misses encoded values.** `vault.redact` matches the exact string. Also match the value's base64, URL-encoded and JSON-escaped forms, which is how tokens usually show up in logs and tool output.
5. **Pagination in SQL.** `thread_items` (`conv.py:235-255`) loads every item of a thread and slices in Python; `thread_model` and `project_model` run three or more queries per row. Fine at today's size. A `UNION ALL` over the seven sources ordered by time with `LIMIT`, and one grouped query for unread and open-ask counts, before an instance has years of threads.
6. **Bounds on settings.** `Settings.set` accepts `RESERVE_WEEKLY_PCT = 1000`. Add min/max to `SPECS` (part of S8).
7. **Unit-free budget arithmetic.** Points mean different things per provider and the code relies on never mixing them (C4 is one slip). A small `Cost` type that carries dollars and the provider, with points derived for display, would make the mistake impossible. More in the wakeups review.
8. **`tools.py:394` `SCOPE_KINDS` is unused**; `connector.py:41` has the live copy. Delete it.
9. **A real-model smoke suite.** Almost every "unverified" note in `development.md` is the same gap. A small script that runs four or five scenarios against a real account (a wakeup that plans, a Reply now with a question, a gate-mode end, an opencode run, a sidecar chat turn), records the transcript, and checks the control-side outcomes. Run by hand before a release. It would have caught C1 and C2.
10. **Health and disk.** Nothing watches the volumes. The old instance grew to 481 MB of notes; `/work` and `/cache` will be far larger. Report volume sizes and SQLite size on Settings → System and warn before a disk fills, since a full disk breaks the append-only run log that restart safety depends on.
11. **One database connection behind one lock.** `db/__init__.py` shares a single connection between request threads and the event loop. Every read waits for every other, WAL's concurrent readers go unused, and the run manager calls it directly from the event loop, so a slow search on a request thread stalls the live stream and the run follower. Keep the single writer; add a small pool of read-only connections, and move the event loop's database work onto threads.
12. **Daylight-saving tests.** Allowed hours, the fallback's 03:00, routine times and monthly resets are all local-time arithmetic. Add tests for the night the clocks change in both directions (a 02:30 that doesn't exist, a 01:30 that happens twice).
13. **Measure the brief.** `_prompt` appends every project, every routine, every open claim and all unseen news to every run. Record the brief's size on the run, show it, and cap each section with a pointer to the command that lists the rest ("12 more projects: `assistant project list`").
14. **Check that opencode loads the manual and skills.** The seed's manual is `CLAUDE.md` and skills live in `.claude/skills/`. Confirm in the real-model suite that opencode reads both; if not, seed an `AGENTS.md` that points at the manual.
