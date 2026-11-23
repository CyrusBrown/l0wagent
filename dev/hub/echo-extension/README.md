# echo (test extension)

A small SDK 1.1 extension for trying the hub-service path end to end. The fake hub's `echo` service advertises it
(`--echo-extension GIT COMMIT .` once this folder is pushed to a git repository), so joining installs it through
the ops container.

- `POST /api/ext/echo/say {"text": "..."}` calls echo's `echo` method with `ext.hub.acall` and returns the result.
- `ext.hub.on_event("custom", ...)` writes each event echo sends to `/work/_inbox/_ext/echo/events/`.

Tests: `ops/tests/test_echo_extension.py` (install and update through ops), `control/tests/test_echo_extension.py`
(loads and runs it).
