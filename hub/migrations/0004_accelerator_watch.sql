-- Watching active accelerators (protocol/accelerators.md): when an active member's tunnel was first seen with no live
-- connection (unix seconds), or NULL. The scheduled sweep deactivates the member once that is long enough ago.
ALTER TABLE accelerators ADD COLUMN down_since INTEGER;
