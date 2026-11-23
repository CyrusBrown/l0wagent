-- H6, the ops container (ops.py).
--
-- A hub service's extension gets its own switch and install state. `doc_json` is the service document exactly as the hub signed
-- it (the capabilities' `extras` entry): ops checks it against the pinned hub key before it installs anything for a service.
-- ext_state: off | installing | installed | waiting (a newer commit is waiting for the owner's OK) | error.
ALTER TABLE hub_services ADD COLUMN doc_json TEXT;
ALTER TABLE hub_services ADD COLUMN extension_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE hub_services ADD COLUMN ext_state TEXT NOT NULL DEFAULT 'off';
ALTER TABLE hub_services ADD COLUMN ext_commit TEXT;      -- the commit that is installed
ALTER TABLE hub_services ADD COLUMN ext_error TEXT;
ALTER TABLE hub_services ADD COLUMN ext_approved TEXT;    -- a commit the owner approved (a card was allowed)
ALTER TABLE hub_services ADD COLUMN ext_asked TEXT;       -- the newest commit a card was filed for (not asked again)

-- Per hub: ask the owner before a newer commit of an installed extension is applied (otherwise it applies and is logged).
ALTER TABLE hub_membership ADD COLUMN ask_before_extension_updates INTEGER NOT NULL DEFAULT 0;

-- Permission actions gain 'extension_update' (control files it itself when a hub publishes a new commit and the owner asked to be
-- asked first; control applies it on approval). Rebuilt as in 0022.
CREATE TABLE permission_actions_new (
  id            INTEGER PRIMARY KEY,
  permission_id INTEGER NOT NULL REFERENCES permissions(id),
  kind          TEXT NOT NULL CHECK (kind IN ('egress_rule', 'secret', 'setting', 'routine', 'mcp_call', 'wake', 'extension_update')),
  payload_json  TEXT NOT NULL,
  applied_at    TEXT,
  error         TEXT,
  result_json   TEXT,
  reverted_at   TEXT
);
INSERT INTO permission_actions_new SELECT id, permission_id, kind, payload_json, applied_at, error, result_json, reverted_at FROM permission_actions;
DROP TABLE permission_actions;
ALTER TABLE permission_actions_new RENAME TO permission_actions;
