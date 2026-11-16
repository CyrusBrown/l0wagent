-- 0004: what M3's shelf watcher needs: the last state seen for each live item.
ALTER TABLE shelf_items ADD COLUMN last_sha256 TEXT;
ALTER TABLE shelf_items ADD COLUMN last_mtime REAL;
ALTER TABLE shelf_items ADD COLUMN last_size INTEGER;
ALTER TABLE shelf_items ADD COLUMN missing INTEGER NOT NULL DEFAULT 0;
CREATE INDEX shelf_items_project ON shelf_items (project_id, removed_at);
