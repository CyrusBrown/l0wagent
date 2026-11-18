# fakehub: a fake l0wagent hub for development

A small Python hub that implements the whole core of the hub protocol (`protocol/*.md`, `l0w/0.1`): capabilities,
invites, registration (tunnel and own-URL), self updates with rotation, leaving, the signed directory, the
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
hub's key survives restarts), and for `serve` `--listen` and `--port`.

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
- Own-URL registration checks that both URLs are HTTPS and different origins, and that the instance URL serves
  the member's own `self` document. TLS is verified (`Config.own_url_verify_tls`).
- A refused registration does not use up the invite.

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
