-- H6 (hubservices.py; protocol/services.md): the hub's services as this instance sees them, and the calls made to them.
-- A service row follows the hub's capabilities document: it appears when the hub lists a verified service document and is
-- removed when the hub drops it. `enabled` is the owner's switch (starts as the service's own default); `state` is
-- where the connection stands. Role grants are control's: they are never shown to the agent or sent to the web app.

CREATE TABLE hub_services (
  address        TEXT PRIMARY KEY,               -- name@hub-domain, lowercase
  kind           TEXT NOT NULL,                  -- relay, rooms, board, email, spaces, directory, admin or custom
  title          TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  docs           TEXT,                           -- an https URL
  roles_json     TEXT NOT NULL DEFAULT '[]',     -- the roles it may grant
  extension_json TEXT,                           -- {git, commit, path}: the instance-side extension it names, if any
  default_on     INTEGER NOT NULL DEFAULT 1,
  enabled        INTEGER NOT NULL DEFAULT 1,     -- the owner's switch
  state          TEXT NOT NULL DEFAULT 'off' CHECK (state IN ('off', 'connecting', 'connected', 'error')),
  error          TEXT,
  connected_at   TEXT,
  grants_json    TEXT NOT NULL DEFAULT '[]',     -- [{role, doc, exp}]: role-grant documents the service signed for us
  sign_key_json  TEXT,                           -- the service's pinned signing key, as the gateway reported it
  present        INTEGER NOT NULL DEFAULT 1,     -- 0: the hub dropped it and our goodbye hasn't been sent yet
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE service_calls (
  id          TEXT PRIMARY KEY,                  -- the call id (base64url of 16 random bytes)
  address     TEXT NOT NULL,
  method      TEXT NOT NULL,
  args_json   TEXT NOT NULL,                     -- redacted
  role        TEXT,                              -- the role named for it (never the grant)
  by          TEXT NOT NULL,                     -- agent | owner | extension:<name>
  state       TEXT NOT NULL CHECK (state IN ('pending', 'ok', 'error', 'timeout')),
  result_json TEXT,                              -- redacted: the result, or {"error": {code, detail}}
  created_at  TEXT NOT NULL,
  done_at     TEXT
);
CREATE INDEX service_calls_created ON service_calls (created_at);
