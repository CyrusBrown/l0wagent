-- The MCP connector (M6): outside chat services act as the 1:1 chat through /mcp, with OAuth 2.1.
-- Clients register themselves (RFC 7591); the owner consents in the web app; tokens are opaque and
-- stored only as hashes. Each grant has its own chat, where its cards and tool calls appear.
CREATE TABLE oauth_clients (
  id            INTEGER PRIMARY KEY,
  client_id     TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  redirect_uris TEXT NOT NULL,             -- JSON list
  created_at    TEXT NOT NULL
);

CREATE TABLE oauth_grants (
  id            INTEGER PRIMARY KEY,
  client_id     TEXT NOT NULL REFERENCES oauth_clients(client_id),
  scopes        TEXT NOT NULL,             -- space-separated: read write wake
  chat_id       INTEGER REFERENCES chats(id),
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  revoked_at    TEXT
);

CREATE TABLE oauth_codes (
  code_hash     TEXT PRIMARY KEY,
  grant_id      INTEGER NOT NULL REFERENCES oauth_grants(id),
  redirect_uri  TEXT NOT NULL,
  challenge     TEXT NOT NULL,             -- PKCE S256
  resource      TEXT,
  expires_at    TEXT NOT NULL,
  used_at       TEXT
);

CREATE TABLE oauth_tokens (
  token_hash    TEXT PRIMARY KEY,
  grant_id      INTEGER NOT NULL REFERENCES oauth_grants(id),
  kind          TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
  expires_at    TEXT NOT NULL,
  revoked_at    TEXT
);
CREATE INDEX oauth_tokens_grant ON oauth_tokens (grant_id);
