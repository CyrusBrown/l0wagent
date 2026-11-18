# Conformance suite for the hub protocol (`l0w/0.1`)

It plays a fake instance (its own keys, its own documents) and checks a hub against `protocol/*.md`, printing
`PASS` / `FAIL` / `SKIP` / `WARN` per check. Exit code 1 if any check failed. It uses the reference library
(`l0wproto`) to build and verify every document and frame.

```
cd protocol/py && uv sync
uv run python -m conformance --hub https://hub.example --invite-cmd "<shell command that prints a fresh invite>"
```
- `--hub`: the hub's base URL. Everything else (api, tunnel URL, hostnames) comes from its capabilities document and
  its join answers.
- `--invite-cmd`: run once per registration attempt (about twenty times), each time it must print a fresh
  `l0w-invite:...` string (the last such line is used). A refused registration is not assumed to leave the invite
  usable. The command should also accept `--bind NAME` (appended) and print a bound invite (membership.md), or fail
  when it can't (a spec-reserved name, a taken name); a command that fails for the `--bind-name` makes the bound-invite
  checks SKIP.
- `--bind-name NAME` (default `opsname`): a name the hub reserved itself (its own name, or an extra reserved name).
  The suite registers it with a bound invite, so on a hub that was used before it reports a SKIP (the name is taken).
  `--bind-name ""` skips the bound-invite checks.
- `--connect HOST:PORT`: open every connection (HTTP, WebSocket, raw) to that address while keeping the `Host`
  header and TLS name. For a hub whose hostnames do not resolve from where you run the suite.
- `--insecure`: do not verify TLS certificates. `-v`: print the detail of passing checks too.

The package lives here (`protocol/conformance/`). It is importable from the `protocol/py` environment because
`protocol/py/pyproject.toml` lists `..` in hatch's `dev-mode-dirs`, so `uv sync` there puts `protocol/` on the path.
Its extra dependencies (`httpx`, `websockets`) are in that project's `dev` group. The fake hub (`dev/hub/`) imports it
the same way for its own tests.

## What is checked
Capabilities (signature, `require_2fa`); invites (lookup does not consume; unknown, used); registration (201, two
different hostnames, hub key; refusals for a tampered body, a missing second signature, another hub, a stale `iat`, an
unsupported version, bad / reserved / lookalike / taken names, a bad invite, an old release, own-URL URLs); the directory
(404 until the first self, then a valid countersigned entry; revocations and reregistrations are arrays); self updates
(higher `seq` only, wrong signer or records refused); rotation (only with a valid chain, signed by the old key or the
recovery key); the tunnel (handshake refusals close 4401, other subprotocol refused, ready limits, PING/PONG,
replacement closes 4409, GET and a streamed POST by both hostnames with `client` set and smuggled headers stripped, the
initial window respected, credit and a streamed response, WebSocket upgrade 501, reset before response 502, body over
the limit 413, `/api/auth/*` rate limit 429, offline 502 with `Retry-After: 30` in JSON and HTML); leaving (200, then 410,
the name never reused, the tunnel closed, the hostnames gone).

Not covered (needs hub-side hooks or time the suite cannot assume): hub-initiated PING after 30 s of silence, the 90 s
dead-connection rule, the 30 s drain limit for a replaced connection, `moved` documents, re-registration approval,
`instance_releases.min` at the tunnel handshake (4426), own-URL success (needs a TLS server the suite would host), and a
peer that overruns a flow-control window (RESET `flow_control`).
