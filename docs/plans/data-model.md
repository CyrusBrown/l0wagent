# Data model and API conventions

A sketch to turn into the real schema and OpenAPI contract in phase 0. Part of the [overhaul plan](README.md).

## API conventions
- **One contract**: an OpenAPI spec. The React app's TypeScript types are generated from it.
- **Auth modes**:
  - session cookie (browser)
  - device token header (Tauri, `l0w` CLI)
  - agent token (sandbox only, `/api/agent/*`)
  - OAuth bearer (MCP clients, `/mcp`)
  - later: signed requests (peers and hub services, `/api/peer/*`, `/api/hub/*`)
- **Live updates**: a streamed response readable with a token header (`fetch` streaming), carrying typed events (message, card, claim, run timeline item, status, unread counts).
- **Offline support**:
  - every create accepts a client-generated **idempotency key**
  - `GET /api/sync?since=<cursor>` returns changes since a cursor
  - `GET /api/meta` gives the server version, capabilities and UI bundle version
  - the UI bundle is downloadable by version (for the desktop app's cache)
- **CORS**: allowed origins include the desktop app's local origin.

## Instance tables

**Conversations**
- `projects` (slug, name, summary, status, doc_path, links, created_by)
- `threads` (project_id NULL = General, title, status, created_by, pinned, archived_at)
- `messages` (thread_id, sender ∈ owner | assistant | chat | system, body, attachments, via, run_id)
- `claims` (run_id, thread_id, plan, status, result)
- `suggestions` (kind, target, payload, status, run_id)
- `shelf_items` (project_id, label, volume, path, mode live|snapshot, keep_versions) + `shelf_versions`
- `uploads`
- `questions`, `permissions` (+ actions), `tasks` (as today, with thread_id)
- `promote_requests`

**Chats**
- `chats`, `chat_messages`, `chat_links` (hand-offs to threads)

**Runs and budgets**
- `runs` (kind ∈ wakeup | reply | routine | reviewer, why, provider, tier, budget, deadline, status, now_sentence, description)
- `sessions` (run_id, runtime, session_id, model, effort)
- `run_events` (raw) and `timeline_items` (normalized)
- `run_notes`
- `jobs` (run_id, pid, label, log, expected_end, dont_interrupt, status)
- `wakeup_plan` (at, budget, tier, reason, created_by, status, rejected_reason)
- `routines`
- `providers`, `roles`, `budgets` (per provider per week, level, boost)
- `usage_ledger` (per session, per provider: tokens, cost, % estimate)
- `usage_snapshots`

**Tools and secrets**
- `secrets` (+ scopes), `secret_reads`
- `connections` (MCP servers, presets, OAuth state), `tool_policies`
- `oauth_clients` and `oauth_tokens` (connected chat services)

**Accounts**
- `users` (username, password hash, TOTP secret), `passkeys`, `sessions`, `devices`, `api_tokens` (scoped)

**System**
- `extensions` (name, version, sdk, source, enabled, last_error)
- `settings`, `kv`, `harness_news`, `notifications`, `push_subscriptions`

**Later, with the hub** (instance side)
- `hub_membership` (hub, address, keys)
- `friends` (address, home hub, pinned key, policies)
- `groups`, `peer_messages` + `outbox`, `presence_cache`, `peer_usage`
- `services` (published: name, type http|stream, target, access, approved), `hubnet_log`
- `shared_projects`, `space_mounts` (local sync state)
- `mail`, `mail_allowlist`

## Hub tables (later; on Cloudflare: D1, R2, Durable Objects)
- `members` (address, instance URL, key + history), `invites`, `signup_requests`
- `linked_hubs`
- `hub_services` (name, type, endpoint, description)
- `services_directory` (member services visible hub-wide)
- Board: `topics`, `posts` (+ attachments), `votes`, `subscriptions`, `discussions` + `discussion_messages`, `watches`
- Per service: `roles`, `audit_log`
- `relay_mailbox`
- `spaces`, `space_members`, `space_versions` (objects in storage), `space_locks`
- `email_routes`, `outbound_mail_log`
