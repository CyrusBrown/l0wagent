# Real-account smoke suite

Drives a running stack through the owner API and checks what control recorded. Unlike the pytest
integration tests it runs against a real, logged-in provider, so it catches what the fake runtime can't:
a changed CLI event format, a prompt the real model misreads, a provider that stopped working.

| | Scenario | Checks |
|---|---|---|
| a | a wakeup that plans the next one | run ends done/idle, spends budget, the reviewer was consulted, "Planned a wakeup" is on the timeline and the plan is on the calendar |
| b | Reply now with a question | a question is filed, the run ends cleanly, the assistant replies after the answer |
| c | gate-mode end | the run is in gate mode, the reviewer was consulted before the end, the run ends done |
| d | an opencode run | skipped without an enabled opencode provider; the run uses it and ends cleanly |
| e | a sidecar chat turn | skipped unless the Claude chat sidecar is available; a reply is stored with its model |

## Run it

```sh
./l0w login                                  # once; the device token is in ~/.config/l0w/config.json
cd control
L0W_URL=http://localhost:8000 L0W_TOKEN=... uv run python ../dev/smoke/smoke.py
uv run python ../dev/smoke/smoke.py --only a,c --budget 10
```

It spends real points (each run is capped by `--budget`, default 15) and takes a few minutes. The
scenarios set standing instructions and the reviewer mode for their run and put both back afterwards,
and cancel the wakeups the assistant planned; the threads and chat they create stay (titled "Smoke: ...").
Don't run it while the assistant is busy: only one run can be active.

Each run's timeline, run detail and the thread or chat transcript are written to
`dev/smoke/out/<date>/real/` (gitignored), with a `summary.json`. Exit code 0 means every scenario passed or was skipped.

## Check the plumbing without an account

```sh
cd control && uv run python ../dev/smoke/smoke.py --dry-run
```

Starts the fake stack (`control/tests/stack.py`: control, runner with the fake claude, egress) in a temp
folder, makes a device token, and runs the same scenarios against it. Scenarios d and e skip there. The
fake can't produce a few real outcomes (marked in the code); those checks are reported as not expected
rather than failed. Recordings go to `dev/smoke/out/<date>/dry-run/`.
