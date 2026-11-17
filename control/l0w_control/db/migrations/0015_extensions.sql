-- Extensions (M8): what control found under extensions/, the owner's on/off, and the last error.
CREATE TABLE extensions (
  name        TEXT PRIMARY KEY,
  version     TEXT NOT NULL,
  sdk         TEXT NOT NULL,
  source      TEXT NOT NULL,            -- 'local' or the git URL from extensions.toml
  enabled     INTEGER NOT NULL DEFAULT 0,  -- found extensions start off: enabling is the owner's action
  last_error  TEXT,
  found_at    TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- An extension's own small data store (ext.store) and its settings (ext.settings). No other table is theirs.
CREATE TABLE extension_kv (
  ext         TEXT NOT NULL,
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (ext, key)
);

-- Inbox cards of an extension's own types; the owner resolves them, the extension's handler acts.
CREATE TABLE extension_cards (
  id          INTEGER PRIMARY KEY,
  ext         TEXT NOT NULL,
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,
  data_json   TEXT NOT NULL,
  thread_id   INTEGER REFERENCES threads(id),
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'withdrawn')),
  resolution_json TEXT,
  created_at  TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX extension_cards_status ON extension_cards (status, created_at);

-- Routines an extension ships; they follow the extension on and off.
ALTER TABLE routines ADD COLUMN extension TEXT;
