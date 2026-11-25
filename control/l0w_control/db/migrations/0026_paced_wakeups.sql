-- Paced wakeups (owner, 2026-11-25): on the Max level control keeps the assistant working through the week, one
-- wakeup per 5-hour window's share, like the old assistant's high-productivity mode. A new wakeup kind, 'paced'.
-- SQLite can't change a CHECK, so the table is rebuilt. runs and peer_wakes reference it: with foreign keys deferred,
-- dropping it counts their references as violations, and putting the same rows back into a table of the same name
-- clears them again before the commit.
CREATE TABLE wakeups_old AS SELECT * FROM wakeups;
PRAGMA defer_foreign_keys = ON;
DROP TABLE wakeups;
CREATE TABLE wakeups (
  id          INTEGER PRIMARY KEY,
  provider    TEXT NOT NULL,
  at          TEXT NOT NULL,
  budget      REAL NOT NULL,
  tier        TEXT NOT NULL,
  reason      TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('planned', 'fallback', 'sweep', 'routine', 'owner', 'paced')),
  created_by  TEXT NOT NULL,
  routine_id  INTEGER,
  status      TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'running', 'done', 'missed', 'skipped', 'cancelled')),
  status_reason TEXT,
  run_id      INTEGER REFERENCES runs(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  note        TEXT
);
INSERT INTO wakeups (id, provider, at, budget, tier, reason, kind, created_by, routine_id, status, status_reason, run_id, created_at, updated_at, note)
  SELECT id, provider, at, budget, tier, reason, kind, created_by, routine_id, status, status_reason, run_id, created_at, updated_at, note FROM wakeups_old;
DROP TABLE wakeups_old;
CREATE INDEX wakeups_at ON wakeups (status, at);
