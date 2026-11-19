-- Claude Code reports a conversation's whole cost so far (total_cost_usd), and resuming continues it: runs resume
-- their conversation for every step and the reviewer keeps one per day. The last total seen per conversation, so
-- each turn is charged what it added (budgets.turn_cost).
CREATE TABLE runtime_totals (
  conversation TEXT PRIMARY KEY,
  total_usd REAL NOT NULL,
  updated_at TEXT NOT NULL
);
