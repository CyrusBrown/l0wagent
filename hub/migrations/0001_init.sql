-- The hub's records. Names are never reused: a member that left stays in `members` with status 'left'.
CREATE TABLE members (
  name         TEXT PRIMARY KEY,
  -- the name with 0->o, 1/i->l, 5->s and dashes removed (membership.md "Lookalikes"); unique, so two
  -- names that look alike can never both exist, even when two registrations race
  lookalike    TEXT NOT NULL UNIQUE,
  address      TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('active', 'left')),
  mode         TEXT NOT NULL CHECK (mode IN ('tunnel', 'own-url')),
  keys         TEXT NOT NULL,                  -- JSON {"sign": key, "seal": key, "recover": key}
  kid_history  TEXT NOT NULL,                  -- JSON array of every kid the member has had
  seq          INTEGER NOT NULL DEFAULT -1,    -- seq of the last accepted self document
  self_doc     TEXT,                           -- JSON signed self document; NULL until the first PUT
  hostnames    TEXT NOT NULL,                  -- JSON {"instance", "files"}
  own_urls     TEXT,                           -- JSON or NULL
  joined       INTEGER NOT NULL,               -- unix seconds
  release      TEXT NOT NULL,                  -- the instance release at registration
  left_at      INTEGER,
  moved_to     TEXT,
  moved_doc    TEXT                            -- JSON signed `moved` document, served with 301
);

CREATE TABLE invites (
  code        TEXT PRIMARY KEY,
  expires     INTEGER NOT NULL,                -- unix seconds
  used_by     TEXT,                            -- the name that used it; NULL while unused
  name_hint   TEXT,
  reregister  TEXT,                            -- a name this invite approves re-registering, or NULL
  created     INTEGER NOT NULL
);

-- Signed documents served in arrays, oldest first (identity.md, membership.md).
CREATE TABLE revocations (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  doc   TEXT NOT NULL
);
CREATE INDEX revocations_name ON revocations (name, id);

CREATE TABLE reregistrations (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  doc   TEXT NOT NULL
);
CREATE INDEX reregistrations_name ON reregistrations (name, id);
