-- 0008: files attached to 1:1 chat messages. They stay in control's data dir (chat-files/),
-- never in the agent's storage; a hand-off copies them into _inbox/ like any owner upload.
CREATE TABLE chat_files (
  id          INTEGER PRIMARY KEY,
  chat_id     INTEGER REFERENCES chats(id),
  message_id  INTEGER REFERENCES chat_messages(id),   -- null until a message sends it
  name        TEXT NOT NULL,
  mime        TEXT NOT NULL,
  size        INTEGER NOT NULL,
  path        TEXT NOT NULL,                          -- relative to the data dir
  created_at  TEXT NOT NULL
);
CREATE INDEX chat_files_message ON chat_files(message_id);
