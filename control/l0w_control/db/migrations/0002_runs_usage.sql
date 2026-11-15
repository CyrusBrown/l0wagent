-- 0002: what M1's run manager and usage gate need.

-- Runs: stop requests survive a control restart; resumes after an agent restart are counted;
-- usage at the start, for the run's own delta; the workspace HEAD for the safety-net diff.
ALTER TABLE runs ADD COLUMN stop_requested_at TEXT;
ALTER TABLE runs ADD COLUMN stop_reason TEXT;
ALTER TABLE runs ADD COLUMN resumes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN usage_start_pct REAL;
ALTER TABLE runs ADD COLUMN workspace_head TEXT;
ALTER TABLE runs ADD COLUMN result TEXT;

-- The runner's id for a session (its log), next to the runtime's own id (session_id, for
-- --resume, filled in once the runtime reports it).
ALTER TABLE run_sessions ADD COLUMN runner_session TEXT;
ALTER TABLE run_sessions ADD COLUMN state TEXT NOT NULL DEFAULT 'running';   -- running | exited | lost
ALTER TABLE run_sessions ADD COLUMN returncode INTEGER;

-- Usage readings (Claude subscription: 5-hour and weekly %).
CREATE TABLE usage_snapshots (
  id            INTEGER PRIMARY KEY,
  provider      TEXT NOT NULL DEFAULT 'claude-subscription',
  fetched_at    TEXT NOT NULL,
  source        TEXT NOT NULL,      -- 'oauth' | 'stream' | 'none'
  five_hour_pct REAL,
  five_hour_resets_at TEXT,
  seven_day_pct REAL,
  seven_day_resets_at TEXT,
  error         TEXT
);
CREATE INDEX usage_snapshots_at ON usage_snapshots (fetched_at);

-- rate_limit_events the runtime reports mid-run: the fallback usage source.
CREATE TABLE rate_limit_events (
  id          INTEGER PRIMARY KEY,
  at          TEXT NOT NULL,
  run_id      INTEGER REFERENCES runs(id),
  limit_type  TEXT,                 -- 'five_hour' | 'seven_day' | ...
  status      TEXT,                 -- 'allowed' | 'allowed_warning' | 'rejected'
  resets_at   TEXT,
  utilization REAL                  -- percent
);
CREATE INDEX rate_limit_events_at ON rate_limit_events (limit_type, at);

-- `assistant notify`: notes to its future self, delivered once at the next inbox check.
CREATE TABLE agent_notifications (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER REFERENCES runs(id),
  body         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  delivered_at TEXT
);
