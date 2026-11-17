"""Tests drive the scheduler themselves (test_budgets.py ticks it with a simulated clock);
the background loop would add fallback wakeups and notes to unrelated tests."""

import os

os.environ.setdefault("L0W_SCHEDULER", "0")
# TestClient calls /api/agent from the host "testclient", not the sandbox subnet; test_auth.py turns this off for the guard test.
os.environ.setdefault("L0W_AGENT_API_ANY_ADDRESS", "1")
