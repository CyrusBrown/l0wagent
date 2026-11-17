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

`./l0w` and `compose.yaml` come from this folder, so update them too when a release changes them:
`git fetch --tags && git checkout vX.Y.Z` (the release you're on; `./l0w update` tells you when the
folder doesn't match the images). A checkout of `main` can be ahead of the last release and name
images it doesn't have yet.

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

## Lost your phone and your recovery codes

```sh
./l0w 2fa-reset           # turns off two-factor sign-in and deletes the recovery codes
```

Then sign in with your password and set 2FA up again in Settings → Account. Anyone who can run
`docker compose exec` on the server can do this, which is why it needs no code.

## Disk space

Settings → System shows the free disk space, the database size (with its write-ahead log) and the
size of the `/workspace` and `/work` volumes (refreshed every 10 minutes; `/cache` belongs to the
agent computer and isn't shown). When free space drops below `DISK_WARN_FREE_GB` (default 5, in
Settings or the environment) the page shows a warning and you get one "Low disk space"
notification a day.

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
