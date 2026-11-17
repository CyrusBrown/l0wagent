-- Integrations: a tool's description and schema are hashed so a later change resets it to "ask"
-- and shows "changed since you allowed it"; the owner can mark a server as on their own network.
ALTER TABLE integration_tools ADD COLUMN spec_hash TEXT;
ALTER TABLE integration_tools ADD COLUMN changed_at TEXT;
ALTER TABLE integrations ADD COLUMN allow_private INTEGER NOT NULL DEFAULT 0;
