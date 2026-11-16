-- 0005: M4's providers, budgets, usage ledger, wakeup plan, routines, reviewer and jobs.
-- Points (owner, 2026-11-15): 1 pt = 0.1% of a Claude subscription's week, or $0.10 on an API key.

-- Providers: credentials plus a usage source. Budget settings live in config_json (level,
-- overrides, boost, reply budget, reserve, sweep, fallback); see budgets.py.
CREATE TABLE providers (
  id          TEXT PRIMARY KEY,     -- 'claude-subscription' | 'anthropic-api'
  kind        TEXT NOT NULL,
  label       TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  credential_enc BLOB,              -- API key (Fernet); never in the agent's general environment
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- What each session cost, in the provider's unit and in points. One row per (run, session,
-- bucket), updated as usage arrives; who = 'assistant' | 'reviewer' | 'chat'.
CREATE TABLE usage_ledger (
  id           INTEGER PRIMARY KEY,
  provider     TEXT NOT NULL,
  run_id       INTEGER REFERENCES runs(id),
  session_id   INTEGER REFERENCES run_sessions(id),
  who          TEXT NOT NULL DEFAULT 'assistant',
  bucket       TEXT NOT NULL DEFAULT 'allowance',  -- 'allowance' | 'reply' | 'sweep'
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd     REAL NOT NULL DEFAULT 0,  -- API-equivalent cost (the runtime's own figure when it reports one)
  cost_final   INTEGER NOT NULL DEFAULT 0,  -- 1 once the runtime reported the session's total
  pts          REAL NOT NULL DEFAULT 0,
  at           TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX usage_ledger_at ON usage_ledger (provider, at);
CREATE INDEX usage_ledger_run ON usage_ledger (run_id);

-- The wakeup plan: the assistant's own schedule, plus control's fallback and sweep wakeups,
-- routine occurrences and the owner's own additions.
CREATE TABLE wakeups (
  id          INTEGER PRIMARY KEY,
  provider    TEXT NOT NULL,
  at          TEXT NOT NULL,
  budget      REAL NOT NULL,
  tier        TEXT NOT NULL,
  reason      TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('planned', 'fallback', 'sweep', 'routine', 'owner')),
  created_by  TEXT NOT NULL,        -- 'assistant' | 'owner' | 'control'
  routine_id  INTEGER,
  status      TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'done', 'missed', 'skipped', 'cancelled')),
  status_reason TEXT,
  run_id      INTEGER REFERENCES runs(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX wakeups_at ON wakeups (status, at);

-- Routines: scheduled prompts with their own tier and budget. schedule_json:
-- {"kind": "weekly", "day": 0-6 (Mon=0), "time": "HH:MM"} | {"kind": "daily", "time": "HH:MM"}
-- | {"kind": "before_reset", "hours": 6}
CREATE TABLE routines (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  prompt      TEXT NOT NULL,
  schedule_json TEXT NOT NULL,
  provider    TEXT NOT NULL,
  tier        TEXT NOT NULL,
  budget      REAL NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  builtin     TEXT,                 -- 'weekly_review' for the built-in one
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'proposed', 'declined')),
  created_by  TEXT NOT NULL,
  run_id      INTEGER REFERENCES runs(id),  -- the run that proposed it
  last_run_at TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- Runs: which budget bucket they spend from, the wakeup or routine that started them,
-- reviewer state and tier changes.
ALTER TABLE runs ADD COLUMN bucket TEXT NOT NULL DEFAULT 'allowance';
ALTER TABLE runs ADD COLUMN wakeup_id INTEGER REFERENCES wakeups(id);
ALTER TABLE runs ADD COLUMN routine_id INTEGER REFERENCES routines(id);
ALTER TABLE runs ADD COLUMN level TEXT;
ALTER TABLE runs ADD COLUMN reviewer_mode TEXT NOT NULL DEFAULT 'off';
ALTER TABLE runs ADD COLUMN reviewer_used REAL NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN tier_changes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN switch_tier TEXT;       -- a pending tier change: the session is interrupted and resumed on it
ALTER TABLE runs ADD COLUMN gate_rounds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN idle INTEGER NOT NULL DEFAULT 0;

-- The reviewer's conversation with the agent (and control's updates to it).
CREATE TABLE reviewer_messages (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER REFERENCES runs(id),
  sender      TEXT NOT NULL,        -- 'assistant' | 'reviewer' | 'control'
  kind        TEXT NOT NULL,        -- assistant: 'ask' | 'start' | 'done'; control: 'watch' | 'end'; reviewer: 'reply' | 'quiet'
  body        TEXT NOT NULL,
  reply_to    INTEGER REFERENCES reviewer_messages(id),
  delivered_at TEXT,                -- reviewer replies: when they reached the agent
  created_at  TEXT NOT NULL
);
CREATE INDEX reviewer_messages_run ON reviewer_messages (run_id, id);

-- Permission actions gain 'routine' (an agent-proposed routine, added on approval). SQLite
-- can't change a CHECK constraint, so the table is rebuilt.
CREATE TABLE permission_actions_new (
  id            INTEGER PRIMARY KEY,
  permission_id INTEGER NOT NULL REFERENCES permissions(id),
  kind          TEXT NOT NULL CHECK (kind IN ('egress_rule', 'secret', 'setting', 'routine')),
  payload_json  TEXT NOT NULL,
  applied_at    TEXT,
  error         TEXT,
  result_json   TEXT,
  reverted_at   TEXT
);
INSERT INTO permission_actions_new SELECT id, permission_id, kind, payload_json, applied_at, error, result_json, reverted_at FROM permission_actions;
DROP TABLE permission_actions;
ALTER TABLE permission_actions_new RENAME TO permission_actions;

-- Promote requests: when the decision reached the agent.
ALTER TABLE promote_requests ADD COLUMN delivered_at TEXT;

-- Jobs: what the agent registered as running in the background.
ALTER TABLE jobs ADD COLUMN note TEXT;
ALTER TABLE jobs ADD COLUMN updated_at TEXT;
