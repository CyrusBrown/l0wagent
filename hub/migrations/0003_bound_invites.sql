-- Bound invites (protocol/membership.md): an invite for exactly one name, which may be a name the hub reserved itself.
ALTER TABLE invites ADD COLUMN bind TEXT;
