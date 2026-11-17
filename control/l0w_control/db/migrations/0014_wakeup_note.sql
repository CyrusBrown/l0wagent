-- A note the agent leaves for one planned wakeup (`assistant wake at … --note FILE`); it opens that wakeup's brief.
ALTER TABLE wakeups ADD COLUMN note TEXT;
