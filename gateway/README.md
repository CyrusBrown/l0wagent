# gateway

The instance's only holder of its private keys for the hub protocol (`protocol/*.md`, reference library `l0wproto`). It signs what control and the tunnel client ask it to sign and never hands a private key out. The single exception: `POST /v0/register` returns the recovery key once, and the gateway forgets it (it keeps only the recovery public key).

**Network rule.** The gateway sits on the `hubnet` network with control and the tunnel client, and never on the sandbox network. The agent container can't reach it.

Config (env): `GATEWAY_DATA_DIR` (default `/data`), `GATEWAY_CONTROL_TOKEN` and `GATEWAY_TUNNEL_TOKEN` (both required, non-empty, different; it refuses to start otherwise), `GATEWAY_PORT` (default 8780), and for development and tests `GATEWAY_OUTBOX_MIN_DELAY` (seconds before the first outbox retry, default 30; it doubles to an hour) and `GATEWAY_OUTBOX_INTERVAL` (seconds between outbox passes, default 5). Run: `python -m l0w_gateway`.

State in the data dir (atomic writes, mode 0600): `keys.json` (current sign and seal seeds with public halves, recovery public key, `retired` public keys, `history` of rotation documents), `tunnel.json` (tunnel config or null), `tunnel-status.json`, and for friends `peers.json`, `outbox.json` and `hubs.json` (below). `keys.json` also keeps `retired_seal` (the encryption keys a re-registration replaced, with `retired_at`; they still open envelopes for 8 days).

Auth is `Authorization: Bearer <token>`; each endpoint takes exactly one of the two tokens. Errors are `{"error": code, "detail": text}`: 401 `unauthorized`, 400 `bad_request`, 409 `no_keys` / `no_tunnel`, 403 `wrong_recovery_key` / `wrong_hub`.

| Endpoint | Token | Caller | What it does |
|---|---|---|---|
| `GET /healthz` | none | Docker | `{"ok": true}` |
| `GET /v0/keys` | control | control | public keys (sign, seal, recover) and rotation history |
| `POST /v0/register` | control | control | new identity: fresh keys, registration signed by sign and recovery keys, returns the recovery key once |
| `POST /v0/sign/self` | control | control | self document with the gateway's own keys and history |
| `POST /v0/sign/leave` | control | control | leave document |
| `POST /v0/sign/moved` | control | control | moved document |
| `POST /v0/rotate` | control | control | scheduled rotation signed by the old sign key |
| `POST /v0/recover` | control | control | compromise rotation signed by the recovery key (sent in the body, used in memory only) |
| `POST /v0/hop-sign` | control | control | hop-auth headers for an outgoing request |
| `PUT /v0/tunnel` | control | control | set or clear (`{"config": null}`) the tunnel config |
| `GET /v0/tunnel/status` | control | control | whether a config is set, and the last reported status |
| `GET /v0/tunnel/config` | tunnel | tunnel client | the tunnel config or null |
| `POST /v0/tunnel/hello` | tunnel | tunnel client | signed tunnel-hello for the configured hub |
| `PUT /v0/tunnel/status` | tunnel | tunnel client | report state (idle, connecting, connected, offline, refused), and optionally `accelerator` (`{"state": off/starting/ready/active/error, "since", "error"}`; kept when a report omits it) |
| `POST /v0/tunnel/accelerator` | tunnel | tunnel client | `{"action": enable/activate/deactivate/disable, "service": "http://127.0.0.1:<port>" (enable only), "iat"?}` returns `{"doc": <signed accelerator-request>}` for the configured address and hub (`protocol/accelerators.md`); `iat` is strictly later than the last one signed (kept in memory), so rapid calls are not `stale`; 409 `no_tunnel`, 400 bad action or service |

The tunnel config (`PUT /v0/tunnel`) also takes `"accelerator": {"enabled": bool, "api": http(s) URL or null}` (default `{"enabled": false, "api": null}`); `GET /v0/tunnel/config` returns it.

**Friends and messages** (`protocol/messages.md`; control is the only caller). All take the control token and need the keys and, for sending and opening, the tunnel config (that is where the gateway reads its own address). Errors add fields where noted.

| Endpoint | What it does |
|---|---|
| `GET /v0/peer/fingerprint` | `{"fingerprint"}` of our current signing key |
| `POST /v0/peer/pin` | `{address, fingerprint?, held_token?}`: resolve the hub (its capabilities are pinned on first contact, `hub_key_changed` after), fetch and verify the member's directory entry, check the fingerprint (409 `fingerprint_mismatch`), then pin or refresh the signing key, encryption key, hostnames and seq (a stale seq changes nothing). A different signing key with no rotation chain from the pinned one is 409 `key_changed` with `old_fingerprint` and `new_fingerprint`, and the pin is kept; a valid chain is followed. `held_token` stores the token the friend gave us (from a friend request's body), verified. Returns `{address, fingerprint, hostnames, rotated, sign_key, seal_key}` (the pinned keys; `seal_key` is what control's `member_keys` seals to); 404 `no_such_member`, 502 `unreachable` / `hub_capabilities` / `bad_directory` |
| `POST /v0/peer/token` | `{address}` -> `{address, token}`: our delivery token for a pinned friend (the existing one unless revoked) |
| `POST /v0/peer/revoke` | `{address}`: mark our token for them revoked (their envelopes then get 403 `no_token`) |
| `POST /v0/peer/forget` | `{address}`: drop the friend (their queued outbox entries still deliver) |
| `POST /v0/peer/send` | `{to, kind, body, request_token?}`: needs a pin (409 `not_pinned`) and, except for `friend-request`, their token (409 `no_token`); `request_token` (or an absent `body.token`) puts our token in a friend-request or friend-accept; seals, signs, queues and tries once inline. -> `{id, state, error}` (`id` is the envelope id). A newer presence replaces an older queued one to the same friend (the old entry is deleted) |
| `PUT /v0/peer/services` | `{addresses: [...]}`: the addresses control says are the hub's services (from the capabilities, protocol/services.md). Stored in `services.json` (normalised, sorted). `/open` takes `service-*` envelopes **only** from these, and takes nothing else from them (403 `no_token` either way). `send` of a tokenless `service-hello` needs only a pin; its body carries our token (issued if `request_token` or `body.token` is absent). A `service-welcome` stores the token in its body as the held token, like a friend-accept. `pin`'s answer includes `sign_key` (the pinned signing key object), which control verifies role grants against |
| `GET /v0/peer/outbox?since=<iso>` | `{"entries": [...]}` changed at or after `since`, without `doc` and `host` |
| `POST /v0/peer/open` | the raw envelope bytes as the body (at most 16 MiB, else 413 `too_large`): spec checks 2 to 6 except duplicate ids. Returns `{from, from_kid, fingerprint, id, seq, kind, iat, body}`. A friend-accept's token is stored as the held token; a friend request does not pin its sender. Errors: 403 `no_token`, 401 `bad_signature` (no detail), 404 `not_here`, 400 `bad_seal` / `bad_body` / `bad_document`, 422 `unknown_kind`, 503 `unresolvable` (the sender's directory entry couldn't be fetched: tell the sender to retry). Following a sender's signed rotation updates the pin |


**Shared spaces** (`protocol/spaces.md`; control token, keys and the tunnel config needed).

| Endpoint | What it does |
|---|---|
| `POST /v0/space/open-key` | `{doc: <space-key document>}` -> `{key: base64url}`. The document must name this instance as `member` and one of our seal keys (the current one, or one retired in the last 8 days) as `member_kid`; the gateway builds the HPKE `info` from the document's own fields (`l0wproto.spaces.space_key_info`), so it opens nothing else. It does **not** check the granter's signature: control does. Errors: 400 not a space-key document, 403 `not_for_us`, 422 `cannot_open` |
| `POST /v0/sign/space-version` | `{payload}` -> `{doc: <signed space-version>}`, signed with the signing key. `v`, `type` and `iat` are filled in when absent; only the spec's fields are allowed; `author` must be this instance (403 `not_the_author`); the shape is validated (400) and the document is at most 1 MiB |

`peers.json` (by lowercase address): `{address, sign_key, old_keys, recover_key, sign_seq, seal_key, hostnames, fetched_at, issued_token: {doc, id, revoked} | null, held_token, seq_out}`. `outbox.json`: `[{id, to, kind, doc, host, seq, created, attempts, next_at, state: queued|delivered|failed, error, final_at, updated}]`. `hubs.json`: the hub signing key pinned per hub domain.

Delivery: the envelope is POSTed to `https://<their instance hostname>/api/peer/v0/inbox` (no redirects; the hostname is refreshed from the directory when the pinned copy is over 5 minutes old). A background task retries queued entries (30 s doubling to 1 h); `2xx` (a `200 duplicate` too) is delivered, a `4xx` other than 408/429 fails with their error code, 7 days old fails as `expired`; finished entries are kept 14 days. Every outgoing request is vetted like egress (`safefetch.py`: https, public addresses only, connects to the vetted address); `L0W_HUB_ALLOW_HTTP=1` allows http and skips vetting, and `L0W_HUB_CONNECT_TO=host:port` sends every request there with the real Host header (development). To reach more than one fake hub it also takes a comma-separated list of `domain=host:port` entries (a request to that domain or any name under it goes to that address), with an optional bare `host:port` last for everything else.

**Networking.** Delivery and directory fetches need the internet, so the gateway needs a network that reaches it as well as `hubnet`.

Dev: `uv sync && uv run pytest -q && uv run ruff check .`. Image: `docker build -f gateway/Dockerfile .` from the repo root.
