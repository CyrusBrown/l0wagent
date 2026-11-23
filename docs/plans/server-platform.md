# Server platform

Packaging, install, updates, login, extensions and the security invariants. Part of the [overhaul plan](README.md).

## Packaging

- **Prebuilt multi-arch images on GHCR**, built by GitHub Actions on each tag: control, agent, egress, chat-runtime. With the hub, two more: **gateway** (holds the instance's keys; see [hub.md](hub.md#the-gateway)) and **tunnel** (the spec tunnel client: it dials out to the hub over a WebSocket and signs the hub's challenge; it does nothing in own-URL mode). Channels: `stable` and `edge`.
- `compose.yaml` references the images. `compose.dev.yaml` builds from source, for development and forks.
- **Host resources are per user**: GPU, memory, CPU and host folders go in `compose.override.yaml`.
  - The installer asks the basics and detects `nvidia-container-toolkit`.
  - The 1:1 chat can explain the options and produce a paste-ready snippet (it can't edit host files).
- **Installer** (`./l0w install`):
  1. checks Docker and Compose
  2. picks a free sandbox subnet
  3. generates tokens
  4. pulls the images
  5. starts the stack
  6. prints the URL
- **First-run wizard** in the UI:
  1. owner account (username + password; optional 2FA)
  2. timezone
  3. providers (logins run through the runner, under a lease)
  4. budgets and level
  5. optional hub join (later phase)
- **Docker anywhere**: Linux first. On Docker Desktop (Mac/Windows), the network isolation (internal network, no host IP on the bridge) must be verified, so the isolation checks become a **self-test** the UI can run and show.

## Updates

- Control checks the registry and shows "Update available" with release notes.
- Applying it:
  - `./l0w update` on the host (the default)
  - or the opt-in **ops** container (H6), which does installing and updating from the UI. Docker access goes through a socket proxy limited to this compose project; without it, ops handles extensions only and updates stay on the host.
- **Updates wait for idle**: no new wakeups start, the current run finishes (or reaches a timeout), and long jobs marked "don't interrupt" are waited for.
- **Runs survive a control restart**:
  - The runner writes each session's events to an append-only log, and control re-attaches from its last offset instead of marking the run failed.
  - If the agent container restarts mid-run, control resumes the conversation (`--resume`) once the runner is healthy, telling the agent it was interrupted.
  - The API lease is re-acquired on re-attach.

## Contributing upstream

- PRs to the main repo. CI runs unit tests, a fake-runner integration test and the isolation checks (compose in CI).
- `CHANGELOG.md` and versioned DB migrations (`schema_version`).

## Login, sessions and devices

- **One owner per instance.** Username + password for the owner. Optional **TOTP 2FA** and optional **passkeys** (browsers only; desktop webviews support them unevenly).
- **Browser**: a session cookie. **Apps** (the Tauri app, the `l0w` CLI): a long-lived, revocable **device token**, kept in the OS keychain and sent as a header. Device pairing (approve a code from an existing session) is an alternative way to sign in an app.
- **Hardening**, since the login page may face the internet once a hub is joined:
  - argon2id hashing, a minimum length (12), and a check against known-breached passwords (k-anonymity range API: the password never leaves the server). **The breached-password check is deferred** (owner, 2026-11-14); see the Later list in [development.md](development.md).
  - per-address rate limits, plus a global slow-down for unknown addresses under attack (no account lockout: a stranger could use it to keep the owner out)
  - a push notification and an Inbox card for every new device or session, with one-click revoke
  - a session and device list in Settings
  - a small pre-login surface: the login page, the static bundle, and the machine routes (which authenticate themselves)
  - 2FA (TOTP or a passkey) is required to join a hub
  - device tokens can be scoped (read only; or everything except approvals, secrets and settings), for coding agents using the `l0w` CLI
- **Other callers** authenticate differently (detailed in [chat-and-tools.md](chat-and-tools.md) and [hub.md](hub.md)):
  - outside chat services use OAuth tokens
  - peers and hub services use signed requests
  - the agent uses its agent token, from the sandbox network only

## Extensions

**Model:** the owner (with Claude Code, opencode, Codex…) writes an extension on their host, the way this repo is developed now. The assistant can't write, install or enable extensions. When it wants a harness feature, it files a **task** with a paste-ready spec for a coding agent.

### Where they live
- `extensions/<name>/` next to `compose.yaml`, mounted read-only into control. Or a git URL pinned to a commit, listed in `extensions.toml`.
- Enabled and disabled in Settings. A broken or incompatible extension is disabled with its error shown, and never takes control down.

### Extensions are also how hub features reach the instance
The instance-side parts of **hub services** (email, the board, shared spaces, and whatever services hubs add later) are built as **extensions**, in the services repo. A hub's service extensions install at join (one screen lists them, on by default, each can be switched off); the hub's updates to them apply automatically with a log unless the owner sets that hub to ask first. Core stays small, and a new kind of hub service can come with its own extension instead of a core change. So the SDK must be complete enough for them, which is why it's in the first version:
- **UI**: pages and sidebar sections (e.g. the board), thread types (an Email thread), Inbox card types (outbound mail awaiting approval), previewers, settings panels
- **Agent commands and prompt fragments** (`assistant board …`, board-reading guidance)
- **Calls to hub services through the gateway**, using the instance's identity and any service-role credentials. The extension never sees the instance key.
- **Background jobs** (fetching held mail, syncing a shared space)
- **Managed folders**: an area of the agent's storage the extension may write, stored at `/work/_inbox/_ext/<name>/<folder>/` in SDK v1 and appearing to the agent at `/external/<extension>/<folder>/` from H6, with containment enforced by control
- **Events** in and out (a new mail arrives → a message in a thread)

### What an extension can add (SDK v1)
- **API routes** under `/api/ext/<name>/` for the UI. Agent-facing routes go under `/api/agent/ext/<name>/`, and the request guard still applies.
- **Agent commands**: `assistant ext <name> …` maps to the extension's agent routes.
- **Event hooks**: run started/ended, wakeup planned, message posted, ask decided, file shelved (and peer/hub events once the hub exists).
- **Prompt fragments** in named slots (system, wakeup brief, reviewer, chat).
- **Settings** (shown in Settings automatically), **routines** (scheduled prompts), **connection presets** for Tools & Secrets.
- **UI** (in v1): pages, sidebar sections, project tabs, run-view widgets, thread and card types, Inbox card types, file previewers and settings panels, as web components the app loads.
- **Extra services** via a compose snippet the owner applies (e.g. a CAD render service on the sandbox network).

### Rules
- No direct DB access. Everything goes through the SDK context (`ctx.threads`, `ctx.runs`, `ctx.vault.has()` but never reads…), so upstream schema changes don't break extensions.
- The invariants below still hold for extension routes.
- The manifest declares `sdk = "1.x"`, and control refuses incompatible majors.

### Guide for contributing agents
`extensions/AGENTS.md` and `docs/EXTENDING.md`, written with the SDK:
- the invariants and why they exist
- the SDK reference and examples
- testing against `dev/fake_runner.py`
- never editing core files
- secrets handling
- harness-news entries for agent-facing changes
- the prompt-archive rule
- how to propose an extension upstream into core

**Sharing:** extensions are git repos; a catalog can list them later. Installing one is always the owner's action, with the source shown.

## Security invariants

Carried over from today, and still binding:
- The agent container is on the sandbox network only: no Docker socket, no host mounts beyond explicit read-only grants, no `NET_ADMIN`/`NET_RAW`, no other networks.
- Everything inside the agent container is untrusted. Enforcement lives in control and egress.
- Sandbox clients may only call `/api/agent/*` with the agent token. Agent-facing endpoints never let the agent approve permissions, answer its own questions, finish user tasks, write secrets, change settings or start runs. The ops/owner API is refused from the sandbox.
- Secret values are write-only for people and redacted from everything control stores or renders.
- Calls to model providers from the sandbox are gated by the egress lease. Anything new that makes such calls must hold a lease.
- Prompts are control-owned. Old text is archived before edits, and harness news announces agent-facing changes.
- The usage gate fails closed.
- **The runner answers only control.** `RUNNER_ALLOWED_FROM` (compose sets it to `control`) limits the runner's port to control's address; this stops other containers on the sandbox network, not a process inside the agent container, which can read `RUNNER_TOKEN` and call from localhost.
- **The agent can start its own model session, and control can't prevent it.** The provider login and the open lease are local to the agent container. The runner watches for a model CLI that isn't the session's own and control puts a warning on the run's timeline; `core.md` says not to. The real fix is a model proxy in control.
- **The reviewer** runs as a separate unix user (`reviewer`: no sudo, own home, a scrubbed environment with no agent token, runner token or secrets). It reads `/workspace` and `/work` as "other" and cannot read the agent's home (mode 0700) or its processes' environment. It still has a shell and its model login is a copy of the agent's login file, so "no writes" for the reviewer means: it can't write the agent's files, but it can write its own home and `/tmp`, and it can reach the network through egress. Where the account is missing (development without Docker) it falls back to the agent's own user and is only asked not to write.

New with the overhaul:
- **The agent can't change its harness**: no extension writes, no settings, no secrets, no budget changes. Promoting a reply to a full run, publishing to the public internet, and anything costing extra budget need the owner.
- **The chat runtime holds nothing**: the 1:1 chat's sidecar has no volumes, no secrets and no workspace. API-provider chats run as plain function calls in control, with no shell anywhere.
- **Control's writes into the agent's storage are narrow**: only the owner's uploads into `_inbox/` folders, and extensions' managed folders (e.g. shared spaces). Everything else control reads read-only.
- **Files are served safely**: control resolves agent paths with symlink-safe containment, and agent-made HTML/SVG renders only in a sandboxed iframe on a separate origin. Agent markdown is sanitized.
- **Outside callers authenticate per kind**: the owner (password + session or device token), outside chat services (OAuth scopes, never secrets or approvals), peers and hub services (signatures, later).
- **Approvals stay online**: offline-queued actions never include approvals, secrets, settings or budget changes.

With the hub (planned 2026-11-16; the gateway, the tunnel and joining built in H3, 2026-11-18):
- **Agent-made content a browser runs loads only from the files hostname** (or the files port on the LAN). Nothing agent-made is served from the instance's own hostname.
- **Callers are classed by where they arrive from**: the sandbox, the chat runtime, the tunnel, the local network. Each class has a list of paths it may reach, default deny. `/api/agent/*` is reachable from the sandbox only.
- **The `ops` container (H6)** installs and updates from the UI. `ops` has no Docker access and is called only by control (on the internal `opsnet`); it uses the internet only for git over https and the release check, and installs extensions only at pinned commits from the owner's action, `extensions.toml` or a hub's signed list. `ops-docker` (opt-in at install) alone holds the Docker socket and offers three fixed commands (status, pull this project's images, start the updater), building every Docker request itself. Control still has no Docker socket. See hub-development.md, H6.
- **The instance's keys live in the gateway**, not in control and never in the agent (built in H3). The gateway is on `hubnet` (control and the tunnel service) and its own `gateway-out` network (the internet, for peer delivery and directory fetches; no other container joins it; every target vetted like egress), never the sandbox network; it hands out no private key, and the recovery key only once, to the owner, without keeping it. Its two tokens split who may ask for what: control signs documents and sets the tunnel's settings; the tunnel only signs a `tunnel-hello` for the configured hub.
- **The tunnel reaches control only on `hubnet`** (by the `control-hub` alias), so everything it forwards arrives from `L0W_TUNNEL_SUBNET` and gets the tunnel caller class. A name control has on several networks could resolve elsewhere and dodge the class.
- **Joining a hub needs two-step sign-in** (TOTP or a passkey), and the hub's key is pinned at join: an unexplained change is an alarm for the owner, never accepted silently.
- **Control and the gateway vet every address they call** (integrations, peers, hub services) the way egress does: no private or internal addresses, https, no redirects.
