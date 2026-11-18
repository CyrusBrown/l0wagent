-- What a device token may do: full (everything), cli (no account, security or secret changes), read (reads only).
-- Browser sessions are always full.
ALTER TABLE auth_sessions ADD COLUMN scope TEXT NOT NULL DEFAULT 'full';
