-- 0007: M6's 1:1 chats. Independent conversations with the owner's own model, which know the
-- assistant only through tools. Usage goes to the ledger as who='chat' (the owner's own use).
CREATE TABLE chats (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL,
  provider    TEXT NOT NULL,
  tier        TEXT NOT NULL,              -- model/effort, e.g. sonnet/medium
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'archived')),
  busy        INTEGER NOT NULL DEFAULT 0, -- a reply is being generated
  handed_off_thread_id INTEGER REFERENCES threads(id),
  context_thread_ids TEXT NOT NULL DEFAULT '[]',  -- threads pulled in as read-only context
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- One entry of a chat: the owner's message, the model's reply, a tool call with its result,
-- a card for the owner to act on, or a system note (hand-offs, errors).
CREATE TABLE chat_messages (
  id          INTEGER PRIMARY KEY,
  chat_id     INTEGER NOT NULL REFERENCES chats(id),
  role        TEXT NOT NULL CHECK (role IN ('owner', 'assistant', 'tool', 'card', 'system')),
  body        TEXT NOT NULL DEFAULT '',   -- redacted text
  data_json   TEXT NOT NULL DEFAULT '{}', -- tool: {name, input, result, is_error, call_id}; card: {kind, ..., state}
  raw_json    TEXT,                       -- assistant: the provider's own content blocks, replayed as-is
  provider    TEXT,
  model       TEXT,
  idempotency_key TEXT UNIQUE,
  created_at  TEXT NOT NULL
);
CREATE INDEX chat_messages_chat ON chat_messages(chat_id, id);

ALTER TABLE usage_ledger ADD COLUMN chat_id INTEGER REFERENCES chats(id);
