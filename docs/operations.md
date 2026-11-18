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
./l0w logs control        # or agent, egress; Ctrl+C to stop following
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

`./l0w backup [-o FILE]` writes one encrypted file (default `l0w-backup-DATE.l0wbak`, mode 600). It
holds control's database (taken consistently while it runs) and keys, the assistant's storage
(home, reviewer home, workspace, work), the egress log and rules, the hub gateway's keys (`gateway-data`), and from the install folder
`.env`, `extensions.toml` and `extensions/`. It doesn't hold `agent-cache` (re-downloadable) or the
images. It asks for a passphrase twice (or reads `L0W_BACKUP_PASSPHRASE`). **Keep the passphrase:
there is no way to open the file without it.** A backup contains your secrets, so treat the file
like them; the encryption (AES-256-GCM, scrypt key) is what protects it in storage.

`./l0w restore FILE [--yes]` asks for confirmation and the passphrase, stops the stack, checks the
whole file first (a wrong passphrase or a damaged file changes nothing), replaces the volumes and
those install files with the backup's, and starts the stack again.

To move to a new machine: install l0wagent there (`git clone`/release folder, Docker), copy the
backup file over, and run `./l0w restore FILE` in the folder. No `./l0w install` is needed first;
the restored `.env` brings the tokens and subnets with it (if the new machine's subnets clash,
edit `SANDBOX_SUBNET` / `CHAT_SUBNET` / `TUNNEL_SUBNET` in `.env` and `./l0w up`). Then `./l0w update` to the
current release if you like.

The volumes are named `l0wagent_*` (`control-data`, `egress-data`, `gateway-data`, `agent-home`, `agent-reviewer`,
`agent-workspace`, `agent-work`, and the cache `agent-cache`).

## The hub services (gateway and tunnel)

Two services support joining a hub (see `docs/plans/hub.md`). They run all the time but do nothing until the owner joins one.

- **gateway** holds the instance's private keys for the hub protocol and signs on request; it never hands a key out. Its state is the `gateway-data` volume, which `./l0w backup` and `restore` include, so **a backup now contains the gateway's keys**: keep the file and its passphrase as carefully as the rest.
- **tunnel** dials out to the hub and forwards visitors' requests to control. It holds no key; the gateway signs its handshake.
- Both sit on `hubnet`, an internal network with its own subnet (`TUNNEL_SUBNET`, picked by `./l0w install`; added to an older `.env` by `./l0w update`). The agent is never on it and can't reach either service. The tunnel is also on `outside` to reach the hub. Control joins `hubnet` under the name `control-hub`, which is how the tunnel reaches it, so control sees those requests as coming from the tunnel subnet (`L0W_TUNNEL_SUBNET`) and treats them as a separate, restricted caller class.
- New `.env` entries: `L0W_GATEWAY_TOKEN` (control to gateway), `L0W_GATEWAY_TUNNEL_TOKEN` (tunnel to gateway), `TUNNEL_SUBNET`. Don't edit them by hand; the services refuse to start with an empty or identical pair.

## Talking to it from the command line

`./l0w login` once, then `./l0w status`, `inbox`, `answer`, `approve`/`deny`, `done`/`decline`,
`threads`, `read`, `send`, `runs`, `run`, `wake`, `stop`, `jobs`, `budget`, `level`, `boost`,
`calendar`, `secrets`, `secret set`. `L0W_URL` and `L0W_TOKEN` override the saved login.
