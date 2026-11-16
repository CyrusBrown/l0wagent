-- 0006: M5's web push subscriptions (one per browser or installed app).
CREATE TABLE push_subscriptions (
  id          INTEGER PRIMARY KEY,
  endpoint    TEXT NOT NULL UNIQUE,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  label       TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  last_ok_at  TEXT,
  failures    INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE notifications ADD COLUMN body TEXT;
ALTER TABLE notifications ADD COLUMN url TEXT;
