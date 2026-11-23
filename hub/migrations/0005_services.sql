-- Hub services (protocol/services.md): the operator registers them through the admin API. Each has a directory
-- entry like a member (its signed self document, countersigned on the way out); the capabilities list a
-- hub-signed service document for each. A service name never collides with a member's.
CREATE TABLE services (
  name           TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  title          TEXT NOT NULL,
  description    TEXT,
  docs           TEXT,
  roles_json     TEXT NOT NULL,                -- JSON array of role names
  extension_json TEXT,                         -- JSON {git, commit, path} or NULL
  default_on     INTEGER NOT NULL,             -- 1 = "on", 0 = "off"
  self_doc       TEXT NOT NULL,                -- JSON signed self document
  seq            INTEGER NOT NULL,             -- its seq
  created_at     INTEGER NOT NULL,             -- unix seconds
  updated_at     INTEGER NOT NULL
);
