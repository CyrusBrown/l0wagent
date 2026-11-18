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

## The `cloudflared` accelerator (`l0w_tunnel/accelerator.py`; spec: `protocol/accelerators.md`)
Active when the gateway's tunnel config has `accelerator: {enabled: true, api: <url>}`; the spec tunnel stays connected throughout
(a change to the accelerator part of the config never reconnects it). After the spec tunnel first reports `connected`:
1. asks the gateway for a signed `enable` (`POST /v0/tunnel/accelerator`) and posts it to `api`; keeps the returned token in memory only, never logged;
2. serves a local listener on `127.0.0.1:8790` (`L0W_ACCELERATOR_PORT`): Host routing like the spec tunnel (instance host to control, files host to the
   files app, else `421`), streaming both ways, `X-L0W-Tunnel-Client` set from `CF-Connecting-IP` only (no such header: `400`), every incoming
   `x-l0w-tunnel-*` and hop-by-hop header dropped, upgrade requests (WebSocket) answered `501`;
3. runs `cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:20241 run` (`L0W_CLOUDFLARED` is the binary) with `TUNNEL_TOKEN` in its
   environment (and nothing else of ours; not in argv); restarts it with backoff 1 s to 60 s if it exits;
4. polls `/ready` every 2 s; ready for 10 s: `activate`; not ready for 30 s while active and the spec tunnel is connected: `deactivate`.
   409 `stale` is signed again and retried once after 1 s; 409 `not_connected` is retried later; 5xx and other failures back off (1 s to 60 s) and show as `error`;
5. config turned off: `disable`, stop `cloudflared` and the listener, state `off`.

State (`off`, `starting`, `ready`, `active`, `error`, with `since` and `error`) goes to the gateway in `PUT /v0/tunnel/status` as `accelerator`.
**Shutdown (SIGTERM)** stops `cloudflared` and the listener but tells the hub nothing: the instance may be restarting, and the next start
re-activates. Until then, visitors of an active member see Cloudflare's error page; that window is accepted.
The image carries a pinned, sha256-checked `cloudflared` (build args `CLOUDFLARED_VERSION`, `CLOUDFLARED_SHA256_AMD64/ARM64`).

## Environment
| Variable | Default | |
|---|---|---|
| `TUNNEL_GATEWAY_URL` | `http://gateway:8780` | the gateway's base URL |
| `TUNNEL_GATEWAY_TOKEN` | required | sent as `Authorization: Bearer` |
| `TUNNEL_CONTROL_URL` | `http://control:8000` | target for the instance hostname |
| `TUNNEL_FILES_URL` | `http://control:8001` | target for the files hostname |
| `TUNNEL_POLL_SECONDS` | `5` | config poll interval |
| `L0W_CLOUDFLARED` | `cloudflared` | the cloudflared binary (tests use a fake) |
| `L0W_ACCELERATOR_PORT` | `8790` | the accelerator's local listener (the `service` sent in `enable`) |

Run: `python -m l0w_tunnel`.

## Network
On the `hubnet` network (with control and the gateway) plus `outside` (to reach the hub); **never** the sandbox network.
Control treats requests from it (`L0W_TUNNEL_SUBNET`) as the tunnel caller class: no `/api/agent/*`, no `/api/chat-runtime/*`,
never "local", client address only from `X-L0W-Tunnel-Client`.

## Development
`cd tunnel && uv run pytest -q && uv run ruff check .` The tests start the fake hub from `dev/hub` (a test-only dependency; the
service never imports it), a fake gateway, a fake cloudflared, a fake hub accelerator endpoint and two target apps. Image: `docker build -f tunnel/Dockerfile .` from the repo root.
