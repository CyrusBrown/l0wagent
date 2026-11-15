-- 0001: first schema, covering milestones M1-M3 (accounts, conversations,
-- asks, runs, files). Budgets, providers, chats, tools and the hub come in
-- later migrations. Conventions:
--   * times are ISO-8601 UTC text ('2026-11-14T02:00:00Z')
--   * JSON columns are text holding JSON, named *_json
--   * every table the UI caches is mirrored into change_log (see below)
--   * no ORM; queries live next to the code that uses them

-- ── System ────────────────────────────────────────────────────────────────
CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE kv (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- The sync feed: one row per change to a cached entity. The cursor clients
-- pass to GET /api/sync is `seq`.
CREATE TABLE change_log (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  entity    TEXT NOT NULL,          -- 'project' | 'thread' | 'message' | 'question' | ...
  entity_id TEXT NOT NULL,
  op        TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
  at        TEXT NOT NULL
);

-- Client-generated idempotency keys for creates (offline outbox).
CREATE TABLE idempotency_keys (
  key         TEXT PRIMARY KEY,
  endpoint    TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE harness_news (
  id         INTEGER PRIMARY KEY,
  date       TEXT NOT NULL,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  shown_in_run_id INTEGER REFERENCES runs(id)
);

CREATE TABLE notifications (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,         -- 'approval_needed' | 'new_device' | ...
  ref_kind   TEXT,
  ref_id     TEXT,
  title      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at    TEXT,
  read_at    TEXT
);

-- ── Accounts (one owner per instance) ─────────────────────────────────────
CREATE TABLE users (
  id            INTEGER PRIMARY KEY CHECK (id = 1),  -- exactly one owner
  username      TEXT NOT NULL UNIQUE,
  display_name  TEXT,
  password_hash TEXT NOT NULL,      -- argon2id
  totp_secret   TEXT,               -- NULL = 2FA off
  timezone      TEXT NOT NULL DEFAULT 'UTC',
  created_at    TEXT NOT NULL,
  password_changed_at TEXT NOT NULL
);

CREATE TABLE passkeys (
  id            INTEGER PRIMARY KEY,
  credential_id TEXT NOT NULL UNIQUE,
  public_key    TEXT NOT NULL,
  sign_count    INTEGER NOT NULL DEFAULT 0,
  label         TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT
);

-- Browser sessions (cookie) and app device tokens share one table: both are
-- revocable credentials listed under Settings → Account.
CREATE TABLE auth_sessions (
  id           TEXT PRIMARY KEY,    -- public id shown in the UI
  kind         TEXT NOT NULL CHECK (kind IN ('session', 'device')),
  token_hash   TEXT NOT NULL UNIQUE,
  label        TEXT NOT NULL,       -- 'Firefox on Linux', 'l0w CLI on mac-mini'
  ip           TEXT,
  user_agent   TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT,                -- NULL for device tokens until revoked
  revoked_at   TEXT,
  confirmed_at TEXT                 -- owner pressed "That was me"
);

CREATE TABLE login_attempts (
  id        INTEGER PRIMARY KEY,
  ip        TEXT NOT NULL,
  username  TEXT,
  ok        INTEGER NOT NULL,
  at        TEXT NOT NULL
);
CREATE INDEX login_attempts_ip_at ON login_attempts (ip, at);

-- Scoped tokens for machine callers (the l0w CLI's coding-agent use, later MCP).
CREATE TABLE api_tokens (
  id          TEXT PRIMARY KEY,
  token_hash  TEXT NOT NULL UNIQUE,
  label       TEXT NOT NULL,
  scopes_json TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at  TEXT
);

-- ── Conversations ─────────────────────────────────────────────────────────
CREATE TABLE projects (
  id          INTEGER PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'archived')),
  doc_path    TEXT,                 -- e.g. 'projects/garden-irrigation.md' in /workspace
  links_json  TEXT NOT NULL DEFAULT '[]',
  created_by  TEXT NOT NULL CHECK (created_by IN ('owner', 'assistant', 'chat')),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE threads (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER REFERENCES projects(id),  -- NULL = General
  title       TEXT NOT NULL,
  type        TEXT NOT NULL DEFAULT 'normal',   -- grows later: 'email', 'peer', extension types
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'archived')),
  created_by  TEXT NOT NULL CHECK (created_by IN ('owner', 'assistant', 'chat')),
  pinned      INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  archived_at TEXT,
  last_read_message_id INTEGER     -- owner's read marker, for unread counts
);
CREATE INDEX threads_project ON threads (project_id, status);

CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER NOT NULL REFERENCES threads(id),
  sender      TEXT NOT NULL,        -- 'owner' | 'assistant' | 'chat' | 'system' (grows: 'peer', 'email')
  body        TEXT NOT NULL,        -- markdown; rendered sanitized
  via         TEXT,                 -- 'web' | 'desktop' | 'cli' | 'chat:<id>' | 'mcp:<client>'
  run_id      INTEGER REFERENCES runs(id),
  delivery    TEXT NOT NULL DEFAULT 'queued' CHECK (delivery IN ('queued', 'delivered', 'seen')),
  created_at  TEXT NOT NULL,
  client_id   TEXT UNIQUE           -- idempotency key from the outbox
);
CREATE INDEX messages_thread ON messages (thread_id, id);

CREATE TABLE attachments (
  id          INTEGER PRIMARY KEY,
  message_id  INTEGER REFERENCES messages(id),
  upload_id   INTEGER REFERENCES uploads(id),
  name        TEXT NOT NULL,
  volume      TEXT NOT NULL CHECK (volume IN ('workspace', 'work')),
  path        TEXT NOT NULL,
  size        INTEGER,
  mime        TEXT
);

-- ── Asks ──────────────────────────────────────────────────────────────────
-- All non-blocking: the agent files them and moves on. It may withdraw them,
-- never resolve them.
CREATE TABLE questions (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER REFERENCES threads(id),
  run_id      INTEGER REFERENCES runs(id),
  kind        TEXT NOT NULL CHECK (kind IN ('yes_no', 'single', 'multi', 'text', 'slot', 'scale')),
  text        TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '[]',
  default_json TEXT NOT NULL,       -- what happens if the owner doesn't answer
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'withdrawn', 'expired')),
  answer_json TEXT,
  note        TEXT,                 -- the owner's optional note ("comment" in the UI)
  created_at  TEXT NOT NULL,
  answered_at TEXT,
  delivered_at TEXT
);

CREATE TABLE permissions (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER REFERENCES threads(id),
  run_id      INTEGER REFERENCES runs(id),
  text        TEXT NOT NULL,
  reason      TEXT,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'allowed', 'denied', 'withdrawn')),
  scope       TEXT CHECK (scope IN ('once', 'project', 'always')),  -- see design/mockup/REVIEW.md #21
  note        TEXT,
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  delivered_at TEXT
);

-- Applied by control on approval, never by the agent.
CREATE TABLE permission_actions (
  id            INTEGER PRIMARY KEY,
  permission_id INTEGER NOT NULL REFERENCES permissions(id),
  kind          TEXT NOT NULL CHECK (kind IN ('egress_rule', 'secret', 'setting')),
  payload_json  TEXT NOT NULL,      -- secret actions carry the name only; the value comes via secure input
  applied_at    TEXT,
  error         TEXT
);

CREATE TABLE tasks (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER REFERENCES threads(id),
  run_id      INTEGER REFERENCES runs(id),
  text        TEXT NOT NULL,
  detail      TEXT,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'declined', 'withdrawn')),
  note        TEXT,
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  delivered_at TEXT
);

CREATE TABLE claims (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  thread_id   INTEGER NOT NULL REFERENCES threads(id),
  plan        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'in_progress', 'done', 'partial', 'dropped')),
  result      TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE suggestions (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER REFERENCES runs(id),
  kind        TEXT NOT NULL CHECK (kind IN (
                'thread_archive', 'thread_rename', 'thread_move', 'thread_split', 'thread_merge',
                'project_split', 'project_merge', 'project_stale', 'project_archive')),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('thread', 'project')),
  target_id   INTEGER NOT NULL,
  text        TEXT NOT NULL,        -- human summary shown on the card
  payload_json TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'dismissed', 'withdrawn')),
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);

CREATE TABLE promote_requests (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  thread_id   INTEGER REFERENCES threads(id),
  reason      TEXT NOT NULL,
  budget      REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'denied', 'expired')),
  note        TEXT,
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);

-- New-session / new-device alerts shown in the Inbox.
CREATE TABLE security_alerts (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES auth_sessions(id),
  text        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'revoked')),
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);

-- ── Runs ──────────────────────────────────────────────────────────────────
CREATE TABLE runs (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind IN ('wakeup', 'reply', 'routine', 'reviewer')),
  why          TEXT NOT NULL,       -- 'planned' | 'manual' | 'fallback' | 'sweep' | 'promoted' | 'routine:<id>' | ...
  why_detail   TEXT,
  provider     TEXT,
  tier         TEXT,                -- 'sonnet/medium'
  budget       REAL,                -- in the run's budget unit (see design/mockup/REVIEW.md #10)
  used         REAL NOT NULL DEFAULT 0,
  deadline     TEXT,
  status       TEXT NOT NULL CHECK (status IN ('starting', 'running', 'done', 'partial', 'stopped', 'failed')),
  now_sentence TEXT,
  description  TEXT,
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  end_reason   TEXT                 -- 'idle' | 'budget' | 'deadline' | 'stopped' | 'error'
);

CREATE TABLE run_sessions (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  runtime     TEXT NOT NULL,        -- 'claude-code' | 'opencode'
  session_id  TEXT NOT NULL,        -- the runtime's own id, for --resume
  model       TEXT,
  effort      TEXT,
  log_path    TEXT NOT NULL,        -- the runner's append-only event log
  log_offset  INTEGER NOT NULL DEFAULT 0,  -- how far control has read; re-attach resumes here
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);

CREATE TABLE run_events (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  session_id  INTEGER REFERENCES run_sessions(id),
  at          TEXT NOT NULL,
  raw_json    TEXT NOT NULL
);
CREATE INDEX run_events_run ON run_events (run_id, id);

CREATE TABLE timeline_items (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,        -- see TimelineItem.kind in the API
  simple      INTEGER NOT NULL DEFAULT 0,  -- also shown on the simple view's status timeline
  text        TEXT NOT NULL,
  data_json   TEXT NOT NULL DEFAULT '{}',
  source_event_id INTEGER REFERENCES run_events(id)
);
CREATE INDEX timeline_items_run ON timeline_items (run_id, id);

CREATE TABLE run_notes (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES runs(id),
  at          TEXT NOT NULL,
  text        TEXT NOT NULL
);

CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER REFERENCES runs(id),
  label       TEXT NOT NULL,
  pid         INTEGER,
  log_path    TEXT,
  expected_end TEXT,
  dont_interrupt INTEGER NOT NULL DEFAULT 0,
  progress    TEXT,
  status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed', 'lost')),
  started_at  TEXT NOT NULL,
  ended_at    TEXT
);

-- ── Files ─────────────────────────────────────────────────────────────────
CREATE TABLE uploads (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER REFERENCES projects(id),   -- NULL = /work/_inbox/
  name        TEXT NOT NULL,
  path        TEXT NOT NULL,        -- relative to /work
  size        INTEGER NOT NULL,
  mime        TEXT,
  sha256      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  client_id   TEXT UNIQUE
);

CREATE TABLE shelf_items (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES projects(id),
  label       TEXT NOT NULL,
  volume      TEXT NOT NULL CHECK (volume IN ('workspace', 'work')),
  path        TEXT NOT NULL,
  mode        TEXT NOT NULL CHECK (mode IN ('live', 'snapshot')),
  keep_versions INTEGER NOT NULL DEFAULT 0,   -- 0 = don't keep; N = keep up to N
  added_by    TEXT NOT NULL CHECK (added_by IN ('owner', 'assistant')),
  run_id      INTEGER REFERENCES runs(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  removed_at  TEXT
);

CREATE TABLE shelf_versions (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES shelf_items(id),
  n           INTEGER NOT NULL,
  blob_path   TEXT NOT NULL,        -- control's own copy (snapshots and kept versions)
  size        INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  run_id      INTEGER REFERENCES runs(id),
  created_at  TEXT NOT NULL,
  UNIQUE (item_id, n)
);

-- ── Secrets (basic, M2; scopes and integrations come in M6) ───────────────
CREATE TABLE secrets (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  value_enc   BLOB NOT NULL,        -- write-only for people; never returned by the API
  scope       TEXT NOT NULL DEFAULT 'agent',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE secret_reads (
  id          INTEGER PRIMARY KEY,
  secret_id   INTEGER NOT NULL REFERENCES secrets(id),
  run_id      INTEGER REFERENCES runs(id),
  at          TEXT NOT NULL
);
