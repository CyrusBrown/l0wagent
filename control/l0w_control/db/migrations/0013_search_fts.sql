-- Full-text search (FTS5, trigram tokenizer = case-insensitive substring match) over message bodies,
-- thread titles and run text. External-content tables kept in step by triggers, so the vault scrub's
-- UPDATEs reach the index too.
CREATE VIRTUAL TABLE messages_fts USING fts5(body, content='messages', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE threads_fts USING fts5(title, content='threads', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE runs_fts USING fts5(description, now_sentence, result, content='runs', content_rowid='id', tokenize='trigram');

CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER messages_fts_au AFTER UPDATE OF body ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER threads_fts_ai AFTER INSERT ON threads BEGIN
  INSERT INTO threads_fts(rowid, title) VALUES (new.id, new.title);
END;
CREATE TRIGGER threads_fts_ad AFTER DELETE ON threads BEGIN
  INSERT INTO threads_fts(threads_fts, rowid, title) VALUES ('delete', old.id, old.title);
END;
CREATE TRIGGER threads_fts_au AFTER UPDATE OF title ON threads BEGIN
  INSERT INTO threads_fts(threads_fts, rowid, title) VALUES ('delete', old.id, old.title);
  INSERT INTO threads_fts(rowid, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER runs_fts_ai AFTER INSERT ON runs BEGIN
  INSERT INTO runs_fts(rowid, description, now_sentence, result) VALUES (new.id, new.description, new.now_sentence, new.result);
END;
CREATE TRIGGER runs_fts_ad AFTER DELETE ON runs BEGIN
  INSERT INTO runs_fts(runs_fts, rowid, description, now_sentence, result) VALUES ('delete', old.id, old.description, old.now_sentence, old.result);
END;
CREATE TRIGGER runs_fts_au AFTER UPDATE OF description, now_sentence, result ON runs BEGIN
  INSERT INTO runs_fts(runs_fts, rowid, description, now_sentence, result) VALUES ('delete', old.id, old.description, old.now_sentence, old.result);
  INSERT INTO runs_fts(rowid, description, now_sentence, result) VALUES (new.id, new.description, new.now_sentence, new.result);
END;

INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
INSERT INTO threads_fts(threads_fts) VALUES ('rebuild');
INSERT INTO runs_fts(runs_fts) VALUES ('rebuild');
