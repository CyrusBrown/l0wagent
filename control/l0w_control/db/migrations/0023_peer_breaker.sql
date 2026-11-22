-- H5: the spend breaker for peer activity (breaker.py). Peer spans: stretches of a run spent on friends' messages
-- (the run's points at the start and the end, so the spend is the difference). A friend's own override: 'off' leaves
-- their spend out of both limits. The breaker's state (today's extra, exempt runs, stopped until, the open card)
-- is one JSON value in kv.

ALTER TABLE friends ADD COLUMN breaker TEXT NOT NULL DEFAULT 'normal' CHECK (breaker IN ('normal', 'off'));

CREATE TABLE peer_spans (
  id         INTEGER PRIMARY KEY,
  run_id     INTEGER NOT NULL REFERENCES runs(id),
  address    TEXT NOT NULL,                      -- the friend whose messages opened it
  start_pts  REAL NOT NULL,                      -- the run's points when their messages were delivered
  end_pts    REAL,                               -- the run's points when it closed; NULL while open
  started_at TEXT NOT NULL,
  last_at    TEXT NOT NULL,                      -- the latest delivery into it (a span closes PEER_SPAN_MINUTES after this)
  ended_at   TEXT,
  closed_by  TEXT CHECK (closed_by IN ('post', 'run_end', 'timeout'))
);
CREATE INDEX peer_spans_run ON peer_spans (run_id, ended_at);
CREATE INDEX peer_spans_started ON peer_spans (started_at);
