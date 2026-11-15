-- 0003: what M2's conversations, asks, delivery and basic secrets need.

-- Delivery to the agent: when each owner message, decision or answer reached it.
ALTER TABLE messages ADD COLUMN delivered_at TEXT;
ALTER TABLE suggestions ADD COLUMN delivered_at TEXT;
ALTER TABLE questions ADD COLUMN scale_labels_json TEXT;

-- Permission actions: what applying one did (e.g. the egress rule's id), so a
-- "once" approval can be undone when its run ends.
ALTER TABLE permission_actions ADD COLUMN result_json TEXT;
ALTER TABLE permission_actions ADD COLUMN reverted_at TEXT;

-- Runs: Reply-now runs belong to a thread; holding a run open for the owner's reply.
ALTER TABLE runs ADD COLUMN thread_id INTEGER REFERENCES threads(id);
ALTER TABLE runs ADD COLUMN waiting_until TEXT;
ALTER TABLE runs ADD COLUMN hold_rounds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_sessions ADD COLUMN result_ok INTEGER;

-- Secrets: tell the agent once that a new one exists; count reads.
ALTER TABLE secrets ADD COLUMN announced_at TEXT;
ALTER TABLE secrets ADD COLUMN reads INTEGER NOT NULL DEFAULT 0;
ALTER TABLE secrets ADD COLUMN last_read_at TEXT;
ALTER TABLE secret_reads ADD COLUMN how TEXT NOT NULL DEFAULT 'get';

CREATE INDEX questions_thread ON questions (thread_id);
CREATE INDEX permissions_thread ON permissions (thread_id);
CREATE INDEX tasks_thread ON tasks (thread_id);
CREATE INDEX claims_thread ON claims (thread_id);
CREATE INDEX messages_undelivered ON messages (delivered_at, sender);
