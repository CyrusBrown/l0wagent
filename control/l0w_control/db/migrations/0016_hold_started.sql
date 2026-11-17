-- When the current wait for the owner's reply began; time spent waiting moves the run's deadline (it doesn't count).
ALTER TABLE runs ADD COLUMN hold_started_at TEXT;
