-- H5 part 2: the agent's side of friends (presence we send, wake requests, the agent asking to wake a friend).

-- What presence we last sent each friend (a friend's view of it: coarse unless the owner opted them in for exact).
ALTER TABLE friends ADD COLUMN presence_sent_json TEXT;
ALTER TABLE friends ADD COLUMN presence_sent_at TEXT;

-- Wake requests, both ways. 'in': a friend's assistant asked to wake ours (outcome = what we answered);
-- 'out': our assistant asked theirs (outcome = what they answered, once they have).
CREATE TABLE peer_wakes (
  id            INTEGER PRIMARY KEY,
  direction     TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  address       TEXT NOT NULL,
  envelope_id   TEXT,                            -- in: the request's envelope id (the reply names it); out: the one we sent
  reason        TEXT NOT NULL DEFAULT '',        -- untrusted text when direction is 'in'
  asked         INTEGER NOT NULL DEFAULT 0,      -- in: the owner was asked (counts against the daily limit)
  permission_id INTEGER REFERENCES permissions(id),
  wakeup_id     INTEGER REFERENCES wakeups(id),
  outcome       TEXT CHECK (outcome IN ('allowed', 'denied', 'later')),
  replied_at    TEXT,                            -- in: when the reply went to the gateway
  created_at    TEXT NOT NULL
);
CREATE INDEX peer_wakes_address ON peer_wakes (address, direction, created_at);
CREATE INDEX peer_wakes_open ON peer_wakes (direction, outcome, replied_at);

-- Permission actions gain 'wake' (a friend asked to wake the assistant; control applies it on approval). Rebuilt as in 0009.
CREATE TABLE permission_actions_new (
  id            INTEGER PRIMARY KEY,
  permission_id INTEGER NOT NULL REFERENCES permissions(id),
  kind          TEXT NOT NULL CHECK (kind IN ('egress_rule', 'secret', 'setting', 'routine', 'mcp_call', 'wake')),
  payload_json  TEXT NOT NULL,
  applied_at    TEXT,
  error         TEXT,
  result_json   TEXT,
  reverted_at   TEXT
);
INSERT INTO permission_actions_new SELECT id, permission_id, kind, payload_json, applied_at, error, result_json, reverted_at FROM permission_actions;
DROP TABLE permission_actions;
ALTER TABLE permission_actions_new RENAME TO permission_actions;
