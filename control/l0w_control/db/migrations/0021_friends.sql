-- H5 (friends.py; protocol/messages.md): friends, their requests, friend codes, the envelope ids seen
-- (for duplicate answers) and what control handed the gateway's outbox. Keys and tokens' bookkeeping stay
-- in the gateway; control keeps what the owner decides. Peer threads are `threads` rows of type 'peer'.

CREATE TABLE friends (
  address        TEXT PRIMARY KEY,               -- lowercase name@hub-domain
  display_name   TEXT NOT NULL,                  -- the owner's name for them (the address until they rename it)
  fingerprint    TEXT,                           -- of the pinned signing key, XXXX-XXXX-XXXX-XXXX
  state          TEXT NOT NULL CHECK (state IN ('requested_out', 'requested_in', 'friends', 'blocked')),
  share          TEXT NOT NULL DEFAULT 'nothing' CHECK (share IN ('nothing', 'shared', 'anything')),
  presence_detail TEXT NOT NULL DEFAULT 'coarse' CHECK (presence_detail IN ('coarse', 'exact')),
  waking         TEXT NOT NULL DEFAULT 'never' CHECK (waking IN ('never', 'ask', 'allow')),
  muted          INTEGER NOT NULL DEFAULT 0,
  thread_id      INTEGER REFERENCES threads(id), -- the peer thread
  presence_json  TEXT,                           -- their latest presence body
  presence_at    TEXT,                           -- the iat of the envelope it came in (an older retry never replaces a newer one)
  same_owner_json TEXT,                          -- their same-owner statement (a hint, never an authorization)
  key_alarm      TEXT,                           -- their key changed with no rotation: the owner re-verifies, never automatic
  checked_at     TEXT,                           -- the last time their directory entry was checked
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE friend_requests (
  id          INTEGER PRIMARY KEY,
  direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  address     TEXT NOT NULL,                     -- the other side
  envelope_id TEXT,                              -- incoming: the request's envelope id; outgoing: the one sent
  note        TEXT NOT NULL DEFAULT '',
  made_by     TEXT NOT NULL DEFAULT 'owner' CHECK (made_by IN ('owner', 'assistant')),
  code_used   INTEGER NOT NULL DEFAULT 0,        -- incoming: it carried a valid code; outgoing: made from a code
  code        TEXT,                              -- outgoing drafts only: the friend code, until the request is sent
  fingerprint TEXT,
  token_json  TEXT,                              -- incoming: the delivery token they gave us (handed to the gateway on approval)
  state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'approved', 'declined', 'sent', 'failed', 'expired')),
  error       TEXT,
  client_id   TEXT UNIQUE,                       -- the owner's idempotency key
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);
CREATE INDEX friend_requests_address ON friend_requests (address, direction, state);

CREATE TABLE friend_codes (
  id            INTEGER PRIMARY KEY,
  secret_sha256 TEXT NOT NULL UNIQUE,            -- of the secret as it travels in a request's body; the secret itself is never kept
  label         TEXT NOT NULL DEFAULT '',
  client_id     TEXT UNIQUE,                     -- the owner's idempotency key (a repeat can't show the code again)
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  used_at       TEXT,
  used_by       TEXT
);

CREATE TABLE peer_envelopes (
  id          INTEGER PRIMARY KEY,
  sender      TEXT NOT NULL,
  envelope_id TEXT NOT NULL,
  kind        TEXT NOT NULL,
  received_at TEXT NOT NULL,
  message_id  INTEGER,                           -- the message it became, for a reply_to
  UNIQUE (sender, envelope_id)
);
CREATE INDEX peer_envelopes_received ON peer_envelopes (received_at);

CREATE TABLE peer_outbox (
  id          INTEGER PRIMARY KEY,
  envelope_id TEXT UNIQUE,                       -- the gateway's id, once it has taken it
  to_address  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  message_id  INTEGER REFERENCES messages(id),
  request_id  INTEGER REFERENCES friend_requests(id),
  state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'queued', 'delivered', 'failed')),
  error       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX peer_outbox_open ON peer_outbox (state);
