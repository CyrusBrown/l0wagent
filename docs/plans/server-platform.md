# Server platform

Packaging, install, updates, login, extensions and the security invariants. Part of the [overhaul plan](README.md).

## Packaging

- **Prebuilt multi-arch images on GHCR**, built by GitHub Actions on each tag: control, agent, egress, agent-web, chat-runtime, plus the `cloudflared` config used once a hub is joined. Channels: `stable` and `edge`.
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
  - or an opt-in `updater` sidecar with the Docker socket. It accepts exactly one authenticated command from control ("pull and recreate to tag X"), and it's root-equivalent on the host, so it stays tiny and auditable.
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
  - argon2id hashing, a minimum length, and a check against known-breached passwords (k-anonymity range API: the password never leaves the server)
  - per-IP and per-account rate limits with backoff, and temporary lockout
  - a push notification and an Inbox card for every new device or session, with one-click revoke
  - a session and device list in Settings
  - a small pre-login surface: the login page, the static bundle, and the machine routes (which authenticate themselves)
  - the wizard encourages 2FA when you join a hub
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
The instance-side parts of **hub services** (email, the board, shared spaces, and whatever services hubs add later) are built as **extensions**. They're first-party, bundled with the image, and enabled when a hub advertises the matching service. Core stays small, and a new kind of hub service can come with its own extension instead of a core change. So the SDK must be complete enough for them, which is why it's in the first version:
- **UI**: pages and sidebar sections (e.g. the board), thread types (an Email thread), Inbox card types (outbound mail awaiting approval), previewers, settings panels
- **Agent commands and prompt fragments** (`assistant board …`, board-reading guidance)
- **Calls to hub services through the gateway**, using the instance's identity and any service-role credentials. The extension never sees the instance key.
- **Background jobs** (fetching held mail, syncing a shared space)
- **Managed folders**: an area of the agent's storage the extension may write (e.g. `/shared/<space>`), with containment enforced by control
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
- The agent container is on the sandbox network only: no Docker socket, no host mounts beyond explicit read-only grants, no `NET_ADMIN`/`NET_RAW`, no other networks. Port 8787 reaches the LAN only through the `agent-web` forwarder.
- Everything inside the agent container is untrusted. Enforcement lives in control and egress.
- Sandbox clients may only call `/api/agent/*` with the agent token. Agent-facing endpoints never let the agent approve permissions, answer its own questions, finish user tasks, write secrets, change settings or start runs. The ops/owner API is refused from the sandbox.
- Secret values are write-only for people and redacted from everything control stores or renders.
- Calls to model providers from the sandbox are gated by the egress lease. Anything new that makes such calls must hold a lease.
- Prompts are control-owned. Old text is archived before edits, and harness news announces agent-facing changes.
- The usage gate fails closed.

New with the overhaul:
- **The agent can't change its harness**: no extension writes, no settings, no secrets, no budget changes. Promoting a reply to a full run, publishing to the public internet, and anything costing extra budget need the owner.
- **The chat runtime holds nothing**: the 1:1 chat's sidecar has no volumes, no secrets and no workspace. API-provider chats run as plain function calls in control, with no shell anywhere.
- **Control's writes into the agent's storage are narrow**: only the owner's uploads into `_inbox/` folders, and extensions' managed folders (e.g. shared spaces). Everything else control reads read-only.
- **Files are served safely**: control resolves agent paths with symlink-safe containment, and agent-made HTML/SVG renders only in a sandboxed iframe on a separate origin. Agent markdown is sanitized.
- **Outside callers authenticate per kind**: the owner (password + session or device token), outside chat services (OAuth scopes, never secrets or approvals), peers and hub services (signatures, later).
- **Approvals stay online**: offline-queued actions never include approvals, secrets, settings or budget changes.
