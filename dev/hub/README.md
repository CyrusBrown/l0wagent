# fakehub: a fake l0wagent hub for development

A small Python hub that implements the whole core of the hub protocol (`protocol/*.md`, `l0w/0.1`): capabilities,
invites, registration, self updates with rotation, leaving, the signed directory, the
tunnel server, and the HTTP front that sends member hostnames down their tunnels. No Cloudflare; it uses the
reference library `protocol/py` (`l0wproto`) for every signature, frame, name and hop rule.

It is both a development stand-in for the real hub (`hub/`, H3) and the first thing the conformance suite
(`protocol/conformance/`) is proven against. `fakehub/client.py` is a reference tunnel client; H3's tunnel
service is modeled on it.

## Run it
```
cd dev/hub && uv sync
uv run python -m fakehub serve                      # http://hub.localhost:8700, state in ./fakehub-state.json
uv run python -m fakehub invite --name-hint mara    # prints  l0w-invite:http://hub.localhost:8700#<code>
```
Options (also `FAKEHUB_*` environment variables): `--base-url` (default `http://hub.localhost:8700`), `--domain`
(default: the base URL's host, the part after `@` in addresses), `--files-domain` (default `hubfiles.localhost`),
`--state`, `--key` (the hub signing key's seed file; created next to the state file on first start, so the
hub's key survives restarts), and for `serve` `--listen` and `--port`. `FAKEHUB_OFFLINE_RETRY_AFTER` (seconds, default 30) sets the offline page's `Retry-After`, which a sender's outbox honors, so a test that waits for a retry sets it to 1.

`invite` writes the same state file while the server runs; the server notices. `invite --reregister NAME` makes an
invite that approves re-registering NAME after both keys were lost (the operator's approval, per membership.md).

## Hostnames
A member `mara` gets `mara.hub.localhost:8700` (instance) and `files--mara.hubfiles.localhost:8700` (files):
two separate parent domains, both under `.localhost`, so browsers resolve them to 127.0.0.1 with no setup.
The port stays in the hostname so a browser reaches the dev server; a real hub's hostnames have none.
Any request whose `Host` is a member's hostname goes down that member's tunnel; everything else is the hub API.

## What it does that is easy to miss
- Tunnel: one live connection per member (a newer one replaces the old, which closes 4409 once its streams end,
  at most 30 s); flow control with the 262 144-byte windows; PING after 30 s of silence, dead at 90 s;
  `client` is the visitor's socket address (no `X-Forwarded-For` is believed); hop-by-hop and `x-l0w-tunnel-*`
  request headers are stripped; 502 `instance_offline` (JSON or HTML by `Accept`) with `Retry-After: 30`;
  413 over `request_body_bytes` (declared or streamed); 501 for WebSocket upgrades; 503 over `tunnel_streams`;
  a per-member sliding minute on `/api/auth/*` (429, not forwarded).
- uvicorn logs `ASGI callable returned without completing handshake` for each refused WebSocket upgrade (the 501
  is sent through its denial-response extension). The answer is right; the log line is uvicorn's.
- A refused registration does not use up the invite.

## The test service `echo` (services.md)
`serve --echo` (or `FAKEHUB_ECHO=1`) runs a built-in service, `echo@<hub domain>`, for developing instances against
the hub-services protocol (`protocol/services.md`) without a real service. It is a directory member with its own
keys (derived from the hub's key seed, so they survive restarts), a self document and the hub's countersignature,
and its hostname (`echo.<domain>`) is served by the hub itself, with no tunnel. The capabilities list it in `extras`
as a signed `service` document (kind `custom`, roles `["tester"]`, default `on`). The name `echo` is reserved.
- `--echo-tester NAME` (repeatable; `FAKEHUB_ECHO_TESTERS=a,b`): members whose `service-welcome` carries a signed
  `tester` role grant. Others get an empty `roles` list.
- `--echo-extension GIT COMMIT PATH` (`FAKEHUB_ECHO_EXTENSION=git,commit,path`): the `extension` reference in its
  service document (an https URL, a 40 or 64 hex commit, a relative path).
  The test extension it can advertise is in `dev/hub/echo-extension/` (SDK 1.1: a route that calls echo with `ext.hub.acall`, and
  an `on_event` handler that writes echo's events to a managed folder). Push that folder to a git repository and pass its URL,
  commit and `.`. Tests: `ops/tests/test_echo_extension.py` (install and update through ops from a signed service document) and
  `control/tests/test_echo_extension.py` (loaded by control's loader). No test wires control, ops and the fake hub together:
  the e2e stack (`control/tests/stack.py`) has no ops container.
- Inbox `https://echo.<domain>/api/peer/v0/inbox`: `service-hello` is accepted only from members of this hub (403
  `not_member` otherwise); everything else needs the token echo issued. Methods: `echo` (returns `args`), `whoami`
  (`{address, roles}`: the roles echo honored from the call's grant), `secret` (needs the `tester` role, else the
  reply is `not_allowed`); an unknown method is `unknown_method`. `service-bye` forgets the member and its token.
- Welcomes and replies are separate envelopes, sealed to the member and posted to the member's instance hostname
  through this hub's own routing (to its listener with the member's `Host`), so the member's tunnel must be up. A
  delivery to an offline member is dropped (no retry). Tokens and members are kept in the state file.
- Not done: following the member's key rotations beyond accepting a token issued to an earlier key of the same member.

## Tests, and the conformance suite
```
cd dev/hub && uv run pytest -q          # unit tests, and the whole conformance suite run against an in-process hub
```
To run the suite by hand against a running fakehub:
```
cd protocol/py && uv run python -m conformance --hub http://hub.localhost:8700 \
  --invite-cmd "cd ../../dev/hub && uv run python -m fakehub invite"
```
(`--connect 127.0.0.1:8700` forces every connection to that address when `*.localhost` names do not resolve for you.)
See `protocol/conformance/README.md`.
