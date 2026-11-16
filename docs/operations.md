# Running l0wagent

Commands run in the l0wagent folder on the server.

## Updating

```sh
./l0w update
```

It pulls the new images while the assistant keeps working, then waits until it's idle: no
new runs start, the current run finishes, and long jobs marked "don't interrupt" end. Then it
recreates the containers and checks they're healthy. The app shows "Update available" when a
new release is out, and "Updating…" while an update waits.

- `--timeout 30`: give up waiting after 30 minutes (default 120) and leave things as they were.
- `--force`: don't wait. A run in progress is resumed after the restart.
- `--version 0.5.1`: pin a version (recorded in `.env`); `--version stable` goes back to releases.

Your data is in Docker volumes and survives updates; `compose.override.yaml` and `.env` are
yours and are never overwritten.

## Starting, stopping, logs

```sh
./l0w ps
./l0w logs control        # or agent, egress, agent-web; Ctrl+C to stop following
./l0w restart
./l0w down                # stop everything (data stays)
./l0w up
```

## Logging the assistant in again

If Claude asks the assistant to sign in again (the Inbox or System page will say usage can't
be read), use Settings → Providers, or on the server:

```sh
./l0w claude-login
```

## Host resources

Memory, CPU, the GPU and host folders for the assistant go in `compose.override.yaml` (see
`compose.override.example.yaml`); then `./l0w up`. Host folders should usually be read-only.

## Backups

Everything lives in Docker volumes named `l0wagent_*`:

| Volume | What |
|---|---|
| `l0wagent_control-data` | your account, threads, runs, settings, secrets (encrypted) |
| `l0wagent_agent-workspace` | the assistant's memory (a git repo) |
| `l0wagent_agent-work` | project work and your uploads |
| `l0wagent_agent-home` | the assistant's home, including its Claude login |
| `l0wagent_egress-data` | the request log and network rules |

`l0wagent_agent-cache` holds re-downloadable data and doesn't need backing up. Back up with
the stack stopped (`./l0w down`), e.g.
`docker run --rm -v l0wagent_control-data:/v -v "$PWD":/b alpine tar czf /b/control-data.tgz -C /v .`.

## Talking to it from the command line

`./l0w login` once, then `./l0w status`, `inbox`, `answer`, `approve`/`deny`, `done`/`decline`,
`threads`, `read`, `send`, `runs`, `run`, `wake`, `stop`, `jobs`, `budget`, `level`, `boost`,
`calendar`, `secrets`, `secret set`. `L0W_URL` and `L0W_TOKEN` override the saved login.
