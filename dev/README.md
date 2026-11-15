# dev/

- **The fake runtime** is `agent/runner/fake_claude.py`: it speaks Claude Code's stream-json, so
  the real runner drives it with no login and no usage. The agent image uses it when
  `FAKE_CLAUDE=1` (the default in `compose.dev.yaml`); the integration tests run the runner
  locally with it (`control/tests/stack.py`).
- **`isolation-test.sh`**: the isolation probes against a running compose stack, from inside the
  agent container. CI runs it; run it yourself after changing networks or on a new Docker host.
