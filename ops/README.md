# ops

The ops container (H6; docs/plans/hub-development.md, "The `ops` container: design"). One image, three commands:

| Command | Service | Reaches | What it is |
|---|---|---|---|
| `python -m l0w_ops` | `ops` (always on) | `opsnet` (control, ops-docker) and `ops-out` (the internet, for git and the release check) | control's fixed command API: resolve a ref to a commit, install or remove an extension at a commit, ask for an update |
| `python -m l0w_ops.docker` | `ops-docker` (compose profile `ops-docker`, opt-in) | `opsnet` only, and the Docker socket | three fixed commands on the socket: `status`, `pull`, `run-updater`; called only by `ops` |
| `l0w-updater TAG` | a one-shot container `ops-docker` starts | the Docker socket and the project folder | runs `./l0w update --version TAG --no-drain` in the project folder and records `extensions/.l0w-update.json` |

Neither service is ever on the sandbox network, `hubnet` or the tunnel's network; the agent can't reach them. Control has no Docker socket.

## `ops` API (port 8795, `Authorization: Bearer $L0W_OPS_TOKEN`, control only)

Errors are `{"error": code, "detail": text}` with a 4xx (5xx for ops' own faults, 502 for git or Docker failing).

- `GET /v0/status` -> `{docker, docker_enabled, channel, extensions: [{name, git, commit, path, source, installed_at}], update, update_progress}`. `docker` is true when ops-docker answers; `update` is the last updater result; `update_progress` is the update this process is starting (`pulling`, `starting`, `updater_started`, `failed`).
- `POST /v0/extensions/resolve {git, ref?}` -> `{commit}` (`git ls-remote`; the owner pins a commit, never a branch).
- `POST /v0/extensions/install {name, git, commit, path?, source, service_doc?, hub_key?}`:
  - `owner`: the owner's own action. The `[extension.NAME]` entry is written or replaced in `extensions/.ops.toml` (other entries and comments kept, atomic write; restored if the install fails).
  - `toml`: `git`, `commit` and `path` must equal the entry in the owner's `extensions.toml` (or `.ops.toml`).
  - `hub`: `service_doc` is the hub's signed service document and `hub_key` the pinned hub signing key; ops verifies the signature (`l0wproto.verify_service`) and that `git`, `commit` and `path` equal the document's `extension` (its path `.` means the repository's top).
  - Git: https only, no credentials, public addresses only (`OPS_ALLOW_PRIVATE_HOSTS=1` allows a LAN git server), a bare cache per URL under `extensions/.cache/`, `git fetch` of the commit, `rev-parse` must give back the same full commit, `git archive` of the tree (or of `path`) read with Python's tarfile (regular files and folders only: no symlinks, hard links, devices, absolute or `..` names; 100 MB and 20000 files at most), then an atomic swap into `extensions/NAME`, with `.l0w-source.json` beside the files. No hooks run. It replaces only an extension ops installed itself.
  - The checkout must hold `extension.toml` naming `NAME`.
- `POST /v0/extensions/remove {name}`: only what ops installed (has `.l0w-source.json`). For `owner` it also drops the `.ops.toml` entry; for `toml` the owner's own file is read-only here and its entry stays (the response says so).
- `POST /v0/update {version}`: only when ops-docker is on. `version` must be a real release (the repository's releases list, `edge` only on the edge channel); then ops-docker pulls the images and starts the updater. Returns `{started: true}` at once; progress is in `/v0/status`.

Installs and removals are serialized by a lock.

## `ops-docker` (port 8796, `L0W_OPS_DOCKER_TOKEN`, ops only)

Not a socket proxy: it builds every Docker request itself and offers only `GET /v0/status`, `POST /v0/pull {tag}` and `POST /v0/run-updater {tag}`. Tags must match `^(edge|\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?)$`. It learns its compose project and the project folder's path on the host from its own container's labels (`com.docker.compose.project`, `.working_dir`), so it needs no mount of the project folder. The updater container it creates has the Docker socket and that folder mounted at the same path, and the labels `com.docker.compose.project`, `.oneoff=True` (so `compose up --remove-orphans`, run by the updater, leaves it alone), `.service` and `l0w.updater=1`. It runs as root with all capabilities dropped, a read-only filesystem and no network but `opsnet`. The socket is root-equivalent on the host: that is why this is opt-in (`./l0w install` asks; `./l0w ops enable-updates|disable-updates` later).

## Config (env)

`L0W_OPS_TOKEN` (required), `L0W_OPS_DOCKER_TOKEN`, `L0W_VERSION` (the channel from `.env`), `L0W_COMPOSE_PROFILES` (`COMPOSE_PROFILES` from `.env`: ops-docker counts as on when listed), `OPS_EXTENSIONS_DIR` (`/extensions`), `OPS_TOML_FILE` (`/project/extensions.toml`), `OPS_DOCKER_URL`, `OPS_GITHUB_API`, `OPS_GITHUB_REPO`, `OPS_PORT`, `OPS_DOCKER_PORT`. Test-only: `OPS_ALLOW_FILE_GIT=1` (accept `file://`; never in compose.yaml).

Tests: `cd ops && uv run pytest -q && uv run ruff check .`. They use local git repositories and a fake Docker API on a unix socket; nothing needs Docker or a network.
