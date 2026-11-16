-- Brokered MCP integrations (M6). Control calls the upstream MCP server with the secret, so the
-- credential never enters the sandbox; the agent reaches each one at /api/agent/mcp/<name>.
CREATE TABLE integrations (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,      -- slug: the MCP server name the agent sees
  title        TEXT NOT NULL,
  preset       TEXT,                      -- 'github' | NULL (custom)
  url          TEXT NOT NULL,             -- the upstream streamable-HTTP MCP endpoint
  auth_secret  TEXT,                      -- a secret's name, sent as a bearer token
  enabled      INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'ok', 'error')),
  last_error   TEXT,
  synced_at    TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE integration_tools (
  id             INTEGER PRIMARY KEY,
  integration_id INTEGER NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  input_schema_json TEXT NOT NULL DEFAULT '{"type":"object"}',
  read_only      INTEGER NOT NULL DEFAULT 0,
  policy         TEXT NOT NULL CHECK (policy IN ('allowed', 'ask', 'blocked')),
  present        INTEGER NOT NULL DEFAULT 1,  -- 0 once the upstream stops listing it (policy kept)
  UNIQUE (integration_id, name)
);

CREATE TABLE integration_calls (
  id             INTEGER PRIMARY KEY,
  integration_id INTEGER NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
  tool           TEXT NOT NULL,
  run_id         INTEGER REFERENCES runs(id),
  args_json      TEXT NOT NULL,             -- redacted, clipped
  outcome        TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'asked', 'blocked')),
  permission_id  INTEGER REFERENCES permissions(id),
  detail         TEXT,
  at             TEXT NOT NULL
);
CREATE INDEX integration_calls_at ON integration_calls (integration_id, id);

-- Permission actions gain 'mcp_call' (a brokered tool call with policy "ask"). Rebuilt as in 0005.
CREATE TABLE permission_actions_new (
  id            INTEGER PRIMARY KEY,
  permission_id INTEGER NOT NULL REFERENCES permissions(id),
  kind          TEXT NOT NULL CHECK (kind IN ('egress_rule', 'secret', 'setting', 'routine', 'mcp_call')),
  payload_json  TEXT NOT NULL,
  applied_at    TEXT,
  error         TEXT,
  result_json   TEXT,
  reverted_at   TEXT
);
INSERT INTO permission_actions_new SELECT id, permission_id, kind, payload_json, applied_at, error, result_json, reverted_at FROM permission_actions;
DROP TABLE permission_actions;
ALTER TABLE permission_actions_new RENAME TO permission_actions;
