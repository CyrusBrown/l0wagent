-- The cloudflared accelerator (protocol/accelerators.md): one row per member that has used it.
-- Ids are stored right after Cloudflare creates each object, so a retry after a failure continues.
-- `last_iat` (unix seconds) outlives a disable: the next request must have a strictly later iat.
CREATE TABLE accelerators (
  name          TEXT PRIMARY KEY,
  last_iat      INTEGER NOT NULL DEFAULT 0,
  tunnel_id     TEXT,
  service       TEXT,
  dns_instance  TEXT,
  dns_files     TEXT,
  route_instance TEXT,
  route_files   TEXT,
  active        INTEGER NOT NULL DEFAULT 0
);
