# Hub development plan

The order of work for the hub, what each stage delivers, and how we know it's done. The design is in [hub.md](hub.md); where the two disagree, the decisions below win until hub.md is brought in line (that's part of H1). Part of the [overhaul plan](README.md).

**Status (2026-11-18):** H1–H4 built as far as the repo goes (owner checks, spec review and the live hub deployment pending). See each stage's status note.

Stages are numbered **H1–H7** so they don't collide with the main milestones. **H4 is the first usable point**: you and a friend join your hub and reach your own UIs at your hub hostnames, and claude.ai connects. Friends and messages come in H5.

## Decisions (2026-11-17)
These change or sharpen [hub.md](hub.md):
1. **The spec is the core; your hub is one implementation.** Workers, D1, R2, Durable Objects, email and the rest describe how your hub is built, not what the spec requires. Nothing in the spec, the instance or `hub/` hard-codes `l0w.run`: a hub's domains are its own configuration.
2. **The core is small**: membership, routing, identity (the signed directory), capabilities and basic protection. **Hub-to-hub linking is not core** (Later). Looking up and friending a member of another hub needs no link.
3. **Two hostnames per member, always**: an **instance** hostname and a **files** hostname. The hub decides the layout (`files--you.hub.com`, `files.you.hub.com`, or two separate domains); the instance gets both in the join response and lists them in its self-description, and nothing ever derives a hostname from a name. The spec requires **different origins** and recommends **different sites** (a nested `files.you.hub.com` is the same site as `instance.you.hub.com`). A member joining with their own URL supplies two. Your hub uses two separate domains.
4. **Connecting**: the spec defines **one tunnel protocol** (the instance dials out to the hub over a WebSocket, proves itself by signing a challenge with its instance key, and the hub sends requests down it by `Host`), plus **"own URL"** for members who already have public HTTPS. Hub-specific accelerators (`cloudflared` on your hub, say) are optional extras a hub may advertise in its capabilities; no instance needs them. The hub terminates TLS; passthrough is Later.
5. **No agent web page.** Port 8787 and the `agent-web` forwarder are removed (H1). Browser-facing published services are an optional hub feature with extra hostnames (H7).
6. **Previews**: agent-made HTML and the like load from the **files hostname** on a hub and from the files port (8001) on the LAN, as today. SVG (as `<img>`), PDFs (pdf.js), images and media render inertly in the UI wherever you are.
7. **No `/shared/` in the base assistant.** Shared spaces and similar features are extensions. Extensions' managed folders appear to the agent at **`/external/<extension>/<folder>/`**.
8. **Joining a hub means trusting its operator with your instance.** "Trusted like an ISP" understates it: a hub's service extensions run inside control. So the hub's service extensions install at join (one screen lists them, on by default, each can be switched off), and the hub's updates to them apply automatically with a log, unless the owner sets that hub to ask first.
9. **An `ops` container** does installing and updating from the UI (H6). It reverses M5's "no updater sidecar"; control still has no Docker socket.
10. **Hub services aren't built here.** This repo has the spec, the instance side, the generic core in `hub/` and the service framework. The relay, admin page, group rooms, shared spaces, board and email, with their extensions, go in a separate repo the owner will make. Your hub's own deployment goes in a private ops repo.
11. **Later**: linking, streams (including `l0w ssh`), a non-Cloudflare hub, TLS passthrough, previews for a standalone instance with a single URL of its own, agent pages rendered inside the UI.

## Repos and layout
The instance keeps the repo root: `compose.yaml`, `./l0w`, `.env` and `extensions/` stay where installs expect them (moving the instance into a subfolder would break every install, down to the compose project name, which comes from the folder). New folders sit beside the existing ones:

```
protocol/   the spec: documents, OpenAPI, JSON schemas, test vectors, the conformance suite   (H2)
dev/hub/    the fake hub: Python, no Cloudflare, runs the conformance suite                   (H2)
gateway/    the instance's gateway container: the only holder of the instance keys             (H3)
tunnel/     the instance's tunnel service: the spec tunnel client                               (H3)
hub/        the generic hub core on Cloudflare (Workers, D1, Durable Objects)                   (H4)
ops/        the ops container: installs and updates from the UI                                (H6)
```

- **Boundaries**: `hub/` and the instance depend only on `protocol/`, never on each other. A CI check fails on any import across that line.
- **Elsewhere**: your hub's configuration, real wrangler IDs, the Cloudflare token, policies, privacy statement and deploy workflow live in a **private ops repo** that pins this one. Hub services and their extensions live in the **services repo**.
- **Releases** (from H4): hub tags are `hub-X.Y.Z` (never `v*`) and never create a GitHub release, because control's update banner reads the latest release; the `:edge` image build ignores `hub/**`, `protocol/**` and `docs/**`; `hub/` is in `.dockerignore`; hub changes go in `hub/CHANGELOG.md`; `hub/CLAUDE.md` scopes the instance's invariants away from Workers code.

## Definition of done (every stage)
Everything in [development.md](development.md#definition-of-done-every-milestone), plus:
- **The conformance suite passes** against the fake hub, and from H4 against `hub/` running locally (`wrangler dev`).
- **The spec in `protocol/` is updated** with anything the stage added, with test vectors for anything signed or sealed.
- **Isolation checks cover the new containers**: the agent can't reach the gateway, tunnel or ops containers, and requests through the tunnel reach only what the tunnel caller class allows.
- **Harness news** for every change the agent will notice, and archived prompt text before edits.
- **Browser tests** run against hostnames under `*.localhost` (browsers resolve them to 127.0.0.1), so two hostnames per member work in Playwright without DNS.

---

## H1: Ready for the internet
**Goal:** an instance that is safe to put on the internet, before any hub exists. Much of the original step 0 is already in (the 2026-11-16 fixes, caller classes, trusted proxies, `__Host-` cookies, passkeys).
- **Remove the agent's web page**: the `agent-web/` image and its compose services, its release and CI entries, `EXPOSE 8787`, `GET /api/web-page` and the `WebPage` schema (backends, mock, fixtures), the Home card and `WebPage.tsx`, the 8787 settings in Playwright and `dev/ui-demo.sh`, `safefetch.py`'s name list, the guard's comment, `test_agent_web.py`, the seed's `boot.sh` example, `prompts/core.md` (archived first) and a harness-news entry. `./l0w update` removes the orphaned `agent-web` container on existing installs (it doesn't pass `--remove-orphans` today). Check the owner's own instance for anything served on 8787 first.
- **The files app on a hostname**: it's served by the hostname it's reached at, not only a port, and control builds preview links from a configured files URL. Middleware sets the sandbox header on **every** response, errors included, with a test.
- **Inert rendering** in the UI: SVG as `<img>`, PDFs with pdf.js, images and media in media elements.
- **Scoped device tokens** (for example read-only, CLI, full), shown and revocable per device.
- **Backups**: `./l0w backup` and `./l0w restore`, encrypted, covering the database, the vault, the workspace and work volumes and (from H3) the instance keys. Restore is tested in CI.
- **2FA before joining**: joining a hub (H3) refuses without TOTP or a passkey; the UI explains why.
- **A tunnel caller class** in the guard: requests from the tunnel's network may reach the UI, the owner API, `/mcp`, `/oauth/*`, `/.well-known/*`, `/api/peer/*`, `/api/hub/*` and `/s/*`, and never `/api/agent/*` or `/api/chat-runtime/*`. The client address comes from the tunnel's header only for requests from that network.
- **Docs**: hub.md, README.md and server-platform.md brought in line with the decisions above; `CLAUDE.md`'s layout and invariants lose `agent-web`.
- **Owner tasks** (outside the repo; start early, they take weeks): register your two domains and submit both to the Public Suffix List.

**Done when:** your instance, exposed through Tailscale Funnel (UI on 443, files on 8443), passes an outside probe checklist (agent and chat-runtime routes unreachable, sign-in rate-limited, cookies `Secure` and `__Host-`, the sandbox header on every files response); a backup restores onto a clean machine and the assistant carries on; and an updated install has no `agent-web` container.

**Status (2026-11-17): built, apart from the owner's checks.** What's in:
- The agent's web page is gone (image, compose services, CI and release entries, API, UI, prompt text with an archive and harness news); `./l0w update` runs `up --remove-orphans`, so the old `agent-web` container goes.
- The files app answers at its own hostname too: control hands every request whose `Host` is `L0W_FILES_ORIGIN`'s host to it (`files_origin.HostDispatch`, refused for the sandbox and chat networks), and preview links use that origin. `SandboxHeaders` puts the sandbox CSP and `nosniff` on every files response, errors and PDFs included.
- Inert rendering: images, audio and video come from `/api/files/raw` (which now supports `Range`), SVG shows as an `<img>` from a blob, PDFs are drawn with pdf.js (`PdfView.tsx`, no scripting). Only agent HTML still uses the files origin.
- Scoped device tokens (`full`, `cli`, `read`): `auth.scope_allows` is checked on every owner route for bearer tokens; Settings → Sessions and devices creates them (shown once) and shows each one's scope.
- Backups: `./l0w backup` / `./l0w restore` (`control/l0w_control/backup.py`, a `backup` tools-profile service): AES-256-GCM frames under a scrypt key; the database (online snapshot), vault key, egress data, agent home, reviewer, workspace and work volumes, `.env`, `extensions.toml` and `extensions/`. Restore verifies everything before replacing anything. Instance keys join the parts list in H3.
- The tunnel caller class (`L0W_TUNNEL_SUBNET`): the UI, owner API and connector paths only; never local (setup needs the code); client address only from `X-L0W-Tunnel-Client`; always https.
- `dev/outside-probe.py`: the outside checklist, run against a Funnel URL.
- Docs: hub.md, README.md, server-platform.md and CLAUDE.md follow the decisions above.

**Moved to H3:** "2FA before joining" lands with the join flow itself (nothing to join yet); the spec's registration step requires it (protocol/membership.md).
**Owner checks left:** expose your instance through Tailscale Funnel and run `python3 dev/outside-probe.py --ui … --files …`; restore a backup onto a clean machine; `./l0w update` an existing install and check there's no `agent-web` container; check nothing on your instance relied on port 8787; register the two domains and submit them to the Public Suffix List.

## H2: The protocol (joining)
**Goal:** the half of the spec that joining needs, written down, with a fake hub that passes its own conformance tests. Messages between instances come in H5.
- **`protocol/`**: the spec as documents, OpenAPI for the hub's core endpoints, and JSON schemas.
  - **Keys**: an Ed25519 signing key and an X25519 encryption key with ids and an algorithm field; rotation signed by the old key; the recovery key; revocation; "moved to" records.
  - **The self-description** (`/.well-known/l0wagent/self`): address, keys with history, both hostnames, an optional URL outside the hub, protocol versions. **The directory entry** is a copy plus the hub's signature.
  - **Capabilities** (`/.well-known/l0wagent/hub`): name, key, policies, protocol versions, tunnel address, optional extras (accelerators, extra hostnames, services).
  - **Membership and join**: invites, the signed registration, name rules (lowercase letters, digits, single dashes; reserved names; never reused), hostname assignment, leaving, and the hub attesting a re-registration when both keys are lost.
  - **The tunnel**: connecting, the signed-challenge handshake, framing (requests and responses streamed both ways, by `Host`), reconnecting, and what the hub answers while an instance is offline. Reusing an existing framing (HTTP/2 in reverse, say) is preferred to inventing one, if the hub can run it.
  - **"Own URL" registration**, with two URLs.
  - **Hop authentication**: HTTP Message Signatures (target URL, body digest, `created`/`expires`, a nonce).
  - **Versions**: every document carries a protocol version; receivers ignore unknown fields; a hub supports the current and the previous instance release.
- **Test vectors** for every signed document, and a **conformance suite** that takes a hub's URL.
- **The fake hub** at `dev/hub/`: the whole core, including the tunnel server, on `*.localhost` hostnames with made-up names.

**Done when:** the fake hub passes the conformance suite, and you've reviewed the spec.

**Status (2026-11-17): built; waiting for your review of the spec.** `protocol/` holds draft `l0w/0.1`: the documents (signing, identity, capabilities, membership, tunnel, hop-auth, versions), JSON schemas, `openapi.json` and test vectors from fixed seeds (all generated by scripts with `--check`), and `protocol/py` (`l0wproto`), the reference library the fake hub, conformance suite and H3's gateway use. The fake hub (`dev/hub/`, `uv run python -m fakehub serve`) passes the conformance suite (`protocol/conformance/`), in CI. Choices to review:
- Signed documents carry the payload as exact bytes (base64url) with a typed signing input, so no JSON canonicalization; the directory entry is the member's `self` document plus a hub countersignature (`hub_sig`).
- The tunnel uses its own small framing (seven frame types, HTTP/2-shaped, per-stream flow control) rather than HTTP/2 in reverse, because a Durable Object can't run an HTTP/2 server; no WebSocket upgrades through it in 0.1.
- Names: 3–32 characters, a reserved list and a lookalike rule (`0→o`, `1/i→l`, `5→s`, dashes ignored) in membership.md.
- Error codes the spec didn't name were filled in by the library (`bad_self`, `stale_seq`, `bad_leave`); they're in `openapi.json`.

## H3: Joining a hub
**Goal:** an instance joins a hub from its UI and is reachable through it. Tested against the fake hub.
- **The gateway container** (`gateway/`): makes the instance keys and the recovery key, signs the registration, the self-description and hop signatures, and never hands a private key out. It's on its own network with control and the tunnel service, never on the sandbox network. Envelopes come in H5.
- **The tunnel service** (`tunnel/`): the spec tunnel client, started with settings from control; in own-URL mode it does nothing. It hands requests to control or the files app by `Host`, as the tunnel caller class.
- **Joining and leaving in the UI**: paste an invite, check 2FA, show the recovery key once, register, receive the two hostnames, and check from outside that both answer and that the directory serves the right key. `./l0w hub join <invite>` as a convenience.
- **The self-description** served by control from the gateway's signature, and the **daily self-check** (a card if the directory's copy differs).
- **Using the hostnames**: preview links on the files hostname; the public URL in push notifications, the connector's OAuth issuer and redirects, and links the assistant sends.
- **Standalone mode**: everything works with no hub; `/api/meta` capabilities switch hub features on when a hub advertises them.
- **Key rotation and recovery** in the UI; backups include the keys.
- **Release**: `gateway` and `tunnel` images on GHCR, in `compose.yaml` and the isolation checks.

**Done when:** a fresh instance joins the fake hub from the UI; you reach it at its instance hostname through the tunnel, with previews from its files hostname; the self-check passes; restarting the gateway, the tunnel or control mid-session loses nothing; and rotating the key, then leaving and rejoining, works.

**Status (2026-11-18): built, apart from the owner's checks.** What's in:
- **`gateway/`** (Starlette, `l0wproto`): makes the signing, sealing and recovery keys at join (fresh keys every join), signs the registration with the signing and recovery keys, the self-description (always with its own keys and history: callers can't choose keys), `leave`, `moved`, rotations (scheduled: old key; recovery: the pasted recovery key, used in memory only) and hop signatures. It returns the recovery key once and keeps only its public half. Two tokens: control's (everything above, the tunnel settings) and the tunnel's (read the settings, sign a `tunnel-hello` for the configured hub only, report status). State in `gateway-data`, 0600.
- **`tunnel/`**: the spec tunnel client, modeled on the fake hub's reference client; reads its settings from the gateway, gets the hello signed there, hands the instance hostname to control and the files hostname to the files app (anything else `421`), sets `X-L0W-Tunnel-Client` from the hub's `client` only, reports `idle/connecting/connected/offline/refused` to the gateway, reconnects with backoff.
- **Control** (`membership.py`, migration 0018, `/api/membership*`): preview an invite (verified capabilities, the invite, what would stop a join), join (refused without TOTP or a passkey; the owner says they made a backup or skip it), publish the self-description and serve it at `/.well-known/l0wagent/self`, the self-check (daily and on demand: the hub key is still the pinned one, the directory serves our current self, both hostnames answer from outside; failures notify and show a Home card), rotate, recover, leave (typed address). `/api/meta` reports `hub` (joined) and `hub_join` (gateway installed).
- **Using the hostnames**: requests that came through the hub (the tunnel's network, or `Host` = the instance hostname) get preview links on the files hostname and the connector's OAuth issuer on the instance hostname; on the LAN nothing changes. The files hostname also routes to the files app inside control (`HostDispatch`).
- **Compose**: `gateway` (hubnet only) and `tunnel` (hubnet + outside); control is on `hubnet` as `control-hub`, and the tunnel uses that alias, so its requests always come from `L0W_TUNNEL_SUBNET`. `./l0w install`/`update` write the two tokens and `TUNNEL_SUBNET`; backups include `gateway-data`.
- **UI**: Settings → Hub (invite, preview, join, the recovery key once, tunnel state, self-check, rotate, recover, leave); the user guide has a "Joining a hub" topic.
- **Browser test**: `web/e2e/h3-hub.spec.ts` (its own stack: `dev_stack.py --hub`, `playwright.hub.config.ts`) joins from Settings → Hub and reaches the instance through the hub. It caught a real bug: the recovery key could be lost when the form unmounted mid-join (fixed with `mutateAsync`).
- **Tests**: `control/tests/test_hub_join.py` runs the fake hub (and, when `hub/` is installed, the real hub core under `wrangler dev`), gateway, tunnel and control as processes: join (2FA refused first), reach the instance and files hostnames through the hub, the directory entry, restart the gateway, the tunnel and control, rotate, recover, leave, rejoin under a new name.

Decisions to review:
- Each join makes a new identity (fresh keys); old public keys stay in the gateway's `retired` list. Rejoining needs a new name (names are never reused).
- The self-check's files-hostname test is "the answer carries the sandbox CSP" (only the files app sends it).
- No harness news: nothing the agent sees changed in H3.
- `./l0w hub join` isn't built: joining is UI-only, because `cli`-scope device tokens are refused `/api/membership` by design (joining is a security change, like the account settings) and the join needs the owner's two-step sign-in anyway.
- Not built: own-URL joining is in the API and UI but untested end to end (it needs public HTTPS); hop signatures are signed by the gateway but nothing uses them until H5.

## H4: Your hub, live
**Goal:** the first usable hub. You and a friend join it and reach your own UIs from anywhere.
- **`hub/`**: the generic core on Cloudflare: membership and invites, the directory and its signature, capabilities, the tunnel server (a Durable Object per member's connection), creating the two hostnames per member (DNS records, certificates), and rate limits on members' `/api/auth/*`. It's configured entirely by settings, with `wrangler.example.toml` and an example config. CI runs the conformance suite against it locally.
- **The release fixes** from [Repos and layout](#repos-and-layout), the import-boundary check and `hub/CLAUDE.md`.
- **The private ops repo** (outside this repo): your hub's settings, its wrangler configuration, the deploy workflow holding the Cloudflare token, the privacy statement, invites only.
- **The connector through the hub**: claude.ai (and other cloud-hosted MCP clients) connect to `/mcp` at your instance hostname.
- **Optional**: `cloudflared` advertised as an accelerator, if the Durable Object tunnel turns out slow or costly. Check Cloudflare's current pricing and limits when building.

**Done when:** `hub/` passes the conformance suite; you and a friend each join from an invite and use your own UI at your instance hostname from a phone on cellular; claude.ai connects to yours; and with the hub down, both instances still work on the LAN.

**Status (2026-11-18): the repo's part is built; the live hub is the owner's (needs the domains, the Cloudflare account and the private ops repo).** What's in:
- **`hub/`** (TypeScript, wrangler 4.164.0; `hub/CLAUDE.md`, `README.md`, `CHANGELOG.md` 0.1.0): a port of `l0wproto` (`src/proto/`, checked against every test vector except hop-auth, which the hub doesn't use yet), the core endpoints and an admin endpoint for invites (`POST /v0/admin/invites`, `ADMIN_TOKEN`; `scripts/invite.mjs`), D1 for members, invites, names and documents, and the tunnel as two Durable Objects: `TunnelConn` (one per connection, random id: it holds the socket for its life, does the handshake, streams with flow control) and `MemberRoute` (one per member: which connection is current, the replacement drain with 4409, the `/api/auth/*` rate limit, leaving). The Worker routes by `Host`. Everything is configuration (`wrangler.example.toml`, `config.example.md`): hub name, base URL, tunnel URL, the two hostname patterns, reserved names, limits, policies, the signing seed.
- **Conformance**: `hub/scripts/conformance.sh` runs the suite against `wrangler dev`: 80 passed, 0 skipped. In CI (`hub` job).
- **Hostnames** come from wildcard DNS records and wildcard Worker routes on the two parent domains (Universal SSL covers one level), so joining makes no DNS calls. Documented, untested on a real zone.
- **Release fixes**: `hub/` is in `.dockerignore`; `release.yml` builds instance images only for `v*` tags and ignores `hub/**`, docs and the spec's documents for `:edge`; hub tags are `hub-X.Y.Z` (never a GitHub release); `dev/check-boundaries.py` (CI) keeps the instance and `hub/` depending only on `protocol/`.
- **The connector through the hub**: control's OAuth issuer, protected-resource metadata and the `401` challenge name the instance hostname for requests that came through the hub (tested through the fake hub in `test_hub_join.py`). claude.ai itself is the owner's check.
- **Hub down**: `test_hub_join.py` kills the hub mid-test: the instance keeps working on the LAN, the tunnel shows offline and the self-check fails, then it reconnects and passes.

Deviations from the fake hub to review: the hub domain keeps a non-default port (dev only); a left member's hostnames answer `410 left` (the fake hub: 404); a malformed `history` entry is `400 bad_self`; the OPEN `target` is normalized by `URL` (Workers can't give the raw path); API JSON bodies are capped at 512 KiB.

**Cost and limits (check before going live)**: a Durable Object holding a WebSocket without hibernation is billed for wall-clock duration: roughly 330k GB-s per always-connected member a month, so one member fits the 400k GB-s allowance and each further one costs about $4/month. If that matters, the next step is the hibernation API or the optional `cloudflared` accelerator (not built). The plan's 100 MB request limit (Free/Pro) is below the default `request_body_bytes` (100 MiB), so the edge answers first. Visitor disconnects on an idle streaming response are noticed at the next write.

**Owner tasks left**: the two domains (and the Public Suffix List, from H1); a Cloudflare account; the private ops repo (your `wrangler.toml` from `wrangler.example.toml`, D1 database id, `HUB_SIGNING_SEED` and `ADMIN_TOKEN` as secrets, the wildcard DNS records and routes, a deploy workflow with the API token, privacy statement and terms); deploy; invite yourself and a friend; join from a phone on cellular; connect claude.ai.

## H5: Friends and messages
**Goal:** assistants (and owners) who are friends message each other, across hubs, end to end encrypted.
- **The spec's second half** in `protocol/`: the envelope (sender address and key id, recipients, message id, sequence number, timestamp, type, protocol version, body hash, signature), bodies sealed with HPKE, delivery tokens, friend requests, presence, wake requests, and their test vectors and conformance tests.
- **The gateway**: seals, signs and delivers, vetting each target like egress (resolved, no private addresses, https only, no redirects); inbound `/api/peer/*` checks the delivery token before parsing, verifies against the pinned key, opens the envelope, applies rate and size limits, and hands control plain, verified messages; an outbox that retries.
- **Friends in control and the UI**: friend codes; requests as owner cards, approved by both owners; who may send requests (anyone / my hub / codes only); fingerprints to compare; pinned keys; key-change cards ("re-verify?", never automatic); same-owner statements; mute, block, report.
- **Per friend**: what the assistant may share (nothing about my projects / shared projects / anything), presence detail (coarse by default), waking (never / ask me / allow).
- **Peer threads**: a `peer` thread type and sender; files in `/work/_inbox/peers/<friend>/`; everything from peers is untrusted data; outbound messages pass through redaction.
- **The spend breaker**: 20% of a run, 5% of the period per day, overridable per friend; the card offers Allow +5% today / Allow for this run / Stop for today. The owner's pause switch for all peer activity.
- **The agent's side**: `assistant` commands for friends, messages and presence; prompt text and harness news; `fake_claude.py` scenarios.
- **UI**: friends, requests and peer threads, designed in the mockup's style.

**Done when:** in CI, two instances on two fake hubs become friends with a code and their assistants trade a message with a file; for real, you and your friend do the same on your hub; a message to an offline instance arrives from the outbox when it returns; the breaker trips and asks you; and a changed key shows a card.

## H6: Hub services and the ops container
**Goal:** a hub's services (built in the services repo) plug into any instance, and installing and updating happen from the UI.
- **The spec for services**: registering a service in the directory (role `hub-service`, signed by the hub), listing it in the capabilities document with an optional **extension reference** (git URL, pinned commit, hash), roles a service grants to members' instances, and service calls as envelopes. No service types are built here.
- **The `ops` container** (`ops/`): a fixed command API that only control can call (install or remove an extension at a commit, update the instance to a version, drain and restart). It's on its own network with control, never the sandbox network or the tunnel. It checks every request: commits come from `extensions.toml` or a joined hub's list, versions must be real releases. Docker access goes through a socket proxy limited to this compose project, and is **opt-in at install**; without it, ops handles extensions only and updates stay on the host. `./l0w update` and `./l0w extensions sync` stay as fallbacks. The security invariants in `CLAUDE.md` and server-platform.md gain a line for it.
- **From the UI**: the update banner gets an Update button; Settings → Extensions installs from a git URL.
- **Hub services at join**: one screen lists the hub's services and their extensions (on by default, each can be switched off); confirming installs them through ops. A hub's later extension updates apply automatically with a log, or ask first if the owner set that hub to. Settings lists each hub service with an on/off switch.
- **The SDK**: `ext.hub.available()` and `ext.hub.call()` work through the gateway; role credentials stay in control; `assistant hubnet call <service> …` for the agent.
- **`/external/<extension>/<folder>/`**: the agent image links extensions' managed folders there; prompt text and harness news.
- **A test service** in `dev/hub/` with a small extension, to prove the path end to end. Real services are built in the services repo.

**Done when:** joining a fake hub that advertises the test service installs its extension from the UI with no host command; the agent calls the service; the hub publishes a new commit and the instance applies it and logs it; and the instance updates to a new release from the Update button.

## H7: Published services
**Goal:** assistants offer HTTP services to friends, groups or the hub; other assistants call them, and people open them in a browser where the hub allows it.
- **The spec**: `/s/<name>/` accepts signed requests only, never a browser; access levels (private / friends / a group or project / this hub); member services in the directory; and, as an optional hub feature, extra browser hostnames per service (created on the instance's signed request, removed on unpublish, capped per member) with **visit tokens** (signed by the viewer's instance, naming the service, valid a minute, usable once).
- **The gateway**: outbound calls from the agent go through egress to the gateway, which signs them; inbound `/s/*` is verified against the access rule and forwarded to the service; both sides log every call and visit.
- **Control and the agent**: `assistant publish <name> --port … --access …`, `assistant hubnet ls|info|call`; the owner is notified of every new or widened service with one-click unpublish; public-internet exposure needs the owner's approval. Opening a friend's service mints a visit token and opens it in a new tab.

**Done when:** your assistant publishes an API to friends and your friend's assistant calls it; you open your friend's web page through a visit token; and unpublishing removes it and its hostname.

## Later
- **Hub-to-hub linking**: operator agreements on policy and shared services. Only needed once services want to share across hubs.
- **Streams**: TCP over WebSockets through the gateways (databases, Jupyter kernels), and owner-only `l0w ssh` into your own assistant's machine through the hub.
- **A non-Cloudflare hub**: a Docker image with the same core API and tunnel server and a reverse proxy with Let's Encrypt; it must pass the same conformance suite.
- **TLS passthrough**, for members who don't want the hub to see their traffic.
- **Previews for a standalone instance with one URL of its own**: the files app under a path with the sandbox header, or `srcdoc` frames.
- **Agent pages inside the UI**: static HTML/JS the agent shelves, opened in the files hostname's sandboxed frame, maybe with a read-only bridge to control.
- **Rendering pages to an image** in the agent container, as an agent tool.
