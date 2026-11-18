# tunnel

The instance's hub tunnel client (H3; spec: `protocol/tunnel.md`). It dials **out** to the hub over a WebSocket, has the
gateway sign the handshake, and forwards each visitor request the hub sends down the tunnel to control (the instance
hostname) or the files app (the files hostname). Any other host gets `421`. It never holds a key.

## What it does
- Polls the gateway (`GET /v0/tunnel/config`) every `TUNNEL_POLL_SECONDS`. No config: reports `idle` and waits. A changed or
  removed config closes the connection and reconnects or stops.
- Connects (subprotocol `l0w-tunnel.0.1`), takes the challenge, gets a signed `tunnel-hello` from the gateway
  (`POST /v0/tunnel/hello`), waits for `ready`, then serves streams. Reports `connecting`, `connected`, `offline`, `refused`
  (close 4401 "handshake refused", 4426 "upgrade required") and `idle` with `PUT /v0/tunnel/status`; a failed report is logged only.
- Reconnects with exponential backoff, 1 s to 60 s with jitter; refusals back off at the maximum. 4409 (replaced) is an ordinary reconnect.
- Forwarding keeps the method, target, headers and the original `Host`; drops hop-by-hop headers and any incoming `x-l0w-tunnel-*`;
  sets `X-L0W-Tunnel-Client` to the visitor address the hub saw. Bodies stream both ways with the protocol's flow control; PING at
  30 s of silence, dead at 90 s. A target that cannot be reached is answered `502`.
- `connect_to` in the config overrides the TCP address (dev with `*.localhost` names).

## Environment
| Variable | Default | |
|---|---|---|
| `TUNNEL_GATEWAY_URL` | `http://gateway:8780` | the gateway's base URL |
| `TUNNEL_GATEWAY_TOKEN` | required | sent as `Authorization: Bearer` |
| `TUNNEL_CONTROL_URL` | `http://control:8000` | target for the instance hostname |
| `TUNNEL_FILES_URL` | `http://control:8001` | target for the files hostname |
| `TUNNEL_POLL_SECONDS` | `5` | config poll interval |

Run: `python -m l0w_tunnel`.

## Network
On the `hubnet` network (with control and the gateway) plus `outside` (to reach the hub); **never** the sandbox network.
Control treats requests from it (`L0W_TUNNEL_SUBNET`) as the tunnel caller class: no `/api/agent/*`, no `/api/chat-runtime/*`,
never "local", client address only from `X-L0W-Tunnel-Client`.

## Development
`cd tunnel && uv run pytest -q && uv run ruff check .` The tests start the fake hub from `dev/hub` (a test-only dependency; the
service never imports it), a fake gateway and two target apps. Image: `docker build -f tunnel/Dockerfile .` from the repo root.
