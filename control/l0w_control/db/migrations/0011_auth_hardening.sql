-- One-time 2FA recovery codes (only hashes are stored).
CREATE TABLE recovery_codes (
  id        INTEGER PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  used_at   TEXT
);
