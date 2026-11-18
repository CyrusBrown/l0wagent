-- H3: membership of a hub (membership.py). One row; private keys live in the gateway, never here.
CREATE TABLE hub_membership (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL CHECK (state IN ('joined', 'left')),
  hub_base TEXT NOT NULL,        -- the hub's base URL (scheme://host)
  hub_domain TEXT NOT NULL,      -- the domain in addresses
  hub_name TEXT NOT NULL,
  hub_key TEXT NOT NULL,         -- the hub signing key pinned at join (JSON)
  capabilities TEXT NOT NULL,    -- the last verified capabilities payload (JSON)
  api TEXT NOT NULL,
  name TEXT NOT NULL,
  address TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('tunnel', 'own-url')),
  hostnames TEXT NOT NULL,       -- {"instance", "files"} exactly as the hub assigned them
  own_urls TEXT,
  tunnel_url TEXT,
  self_doc TEXT,                 -- the current signed self-description, as the hub accepted it
  self_seq INTEGER NOT NULL DEFAULT -1,
  joined_at TEXT NOT NULL,
  left_at TEXT,
  backup TEXT,                   -- 'made' | 'skipped' (the owner's answer at join)
  check_at TEXT,
  check_result TEXT,             -- {"ok", "at", "problems"}
  hub_key_alarm TEXT
);

CREATE TABLE hub_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
