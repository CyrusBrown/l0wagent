# gateway

The instance's only holder of its private keys for the hub protocol (`protocol/*.md`, reference library `l0wproto`). It signs what control and the tunnel client ask it to sign and never hands a private key out. The single exception: `POST /v0/register` returns the recovery key once, and the gateway forgets it (it keeps only the recovery public key).

**Network rule.** The gateway sits on the `hubnet` network with control and the tunnel client, and never on the sandbox network. The agent container can't reach it.

Config (env): `GATEWAY_DATA_DIR` (default `/data`), `GATEWAY_CONTROL_TOKEN` and `GATEWAY_TUNNEL_TOKEN` (both required, non-empty, different; it refuses to start otherwise), `GATEWAY_PORT` (default 8780). Run: `python -m l0w_gateway`.

State in the data dir (atomic writes, mode 0600): `keys.json` (current sign and seal seeds with public halves, recovery public key, `retired` public keys, `history` of rotation documents), `tunnel.json` (tunnel config or null), `tunnel-status.json`.

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
| `PUT /v0/tunnel/status` | tunnel | tunnel client | report state (idle, connecting, connected, offline, refused) |

Dev: `uv sync && uv run pytest -q && uv run ruff check .`. Image: `docker build -f gateway/Dockerfile .` from the repo root.
