-- The owner's switch for the hub's cloudflared accelerator (protocol/accelerators.md); on by default.
-- The accelerator's api URL is read from the stored capabilities.
ALTER TABLE hub_membership ADD COLUMN use_accelerator INTEGER NOT NULL DEFAULT 1;
