# The tunnel

How a member without public HTTPS is reached: the instance's tunnel service dials **out** to the hub
over a WebSocket, proves who it is, and the hub sends visitors' HTTP requests down it. The hub
terminates TLS for both of the member's hostnames.

**Why our own framing?** HTTP/2 in reverse would be the natural reuse, but a hub must be able to run
the server side inside a WebSocket handler (Cloudflare Durable Objects, for one) without an HTTP/2
implementation. So the framing below is HTTP/2-shaped (streams, headers then data, per-stream flow
control) and nothing more: seven frame types.

## Connecting
1. The tunnel service opens `GET <tunnel URL>` (from the join response or capabilities) with
   `Sec-WebSocket-Protocol: l0w-tunnel.0.1`. The hub MUST refuse other subprotocols.
2. The hub sends a **text** message: `{"t": "challenge", "nonce": "<base64url, 32 random bytes>", "hub": "hub.example"}`.
3. The instance answers with a **text** message holding a signed `tunnel-hello` document, payload
   `{"v", "type": "tunnel-hello", "iat", "address", "nonce": "<the nonce>", "hub": "hub.example", "release": "0.9.0"}`,
   signed by its current signing key (kept in the gateway; the tunnel service asks the gateway to sign).
4. The hub checks: the nonce is the one it sent (single use, at most 30 s old), `hub` is itself,
   `iat` within 300 s, the address is an active member, and the signature verifies against
   that member's current signing key. Then it sends `{"t": "ready", "session": "<id>", "limits": {...}}`
   (the same `limits` as capabilities). On failure it closes with code **4401** and no detail.
5. The hub allows **one** live connection per member. A newly verified connection replaces the old one:
   the hub stops opening streams on the old one and closes it with **4409** after its open streams end
   (at most 30 s).

## Frames
After `ready`, every message is **binary**:

```
byte 0      type
byte 1      flags (0 in this version; receivers ignore unknown flags)
bytes 2-5   stream id, unsigned 32-bit big-endian
bytes 6-    payload
```

| Type | Name | Direction | Payload |
|---|---|---|---|
| `0x01` | OPEN | hub → instance | JSON: `{"method", "target", "host", "headers": [[name, value], …], "client": "<visitor IP>", "scheme": "https"}` |
| `0x02` | RESPONSE | instance → hub | JSON: `{"status": 200, "headers": [[name, value], …]}` |
| `0x03` | DATA | both | Body bytes, at most 65 536 per frame |
| `0x04` | END | both | Empty: no more body in this direction |
| `0x05` | RESET | both | JSON: `{"error": "<code>"}`. Abandons the stream in both directions |
| `0x06` | CREDIT | both | 4 bytes, unsigned big-endian: more bytes the receiver will accept on this stream |
| `0x07` | PING / PONG | both | Stream id 0. Payload 8 bytes; flags bit 0 set = PONG, echoing the payload |

- **Streams** are opened only by the hub, with odd ids increasing from 1. One request and its response
  per stream. A stream is finished when END (or RESET) has gone both ways.
- **Header names** are lowercase. The hub MUST drop hop-by-hop headers (`connection`, `keep-alive`,
  `transfer-encoding`, `upgrade`, `te`, `trailer`, `proxy-*`) and any header starting with `x-l0w-tunnel-`
  before sending OPEN. `host` is the hostname the visitor asked for: the member's instance or files
  hostname. `target` is the path and query as received.
- `client` is the visitor's IP as the hub saw it. The instance's tunnel service passes it on as
  `X-L0W-Tunnel-Client` and nothing else from the visitor is believed about the address.
- **Flow control**: each direction of each stream starts with a window of 262 144 bytes. DATA may only
  be sent within the window; the receiver sends CREDIT as it consumes. A peer that overruns a window
  gets RESET `flow_control`. The number of open streams is capped by `limits.tunnel_streams`; the hub
  answers visitors over the cap with `503`.
- **Bodies stream** in both directions: a response may send DATA for as long as it likes (live event
  streams, downloads). Request bodies over `limits.request_body_bytes` are refused by the hub with `413`.
- **WebSocket upgrades** through the tunnel are not part of 0.1 (the hub answers them `501`).
- **Liveness**: the instance MUST send PING after 30 s with nothing received; the hub MAY (a hub that
  lets idle connections sleep pings only while streams are open, and relies on the instance's PINGs
  otherwise). A connection with nothing received for 90 s is dead; the hub may notice that late (at
  the next visitor request) and then treats the member as offline. The instance reconnects with
  exponential backoff from 1 s to 60 s, with jitter.

## What the hub answers
- While the member is connected: whatever the instance answers, adding nothing but standard proxy
  behavior (it MAY add `strict-transport-security`).
- While the member is **offline** (no live connection, or the stream was RESET before a RESPONSE):
  `502` with `Retry-After: 30`; `{"v", "error": "instance_offline"}` when the request's `Accept` prefers
  JSON, otherwise a short HTML page saying the instance is offline. Never a cached copy of the member's pages.
- The hub's own rate limits (capabilities `limits.auth_rate_per_minute` on each member's `/api/auth/*`)
  answer `429` without reaching the instance.

## What the instance's tunnel service does
- Hands each stream to control or to the files app **by `host`**: the files hostname goes to the files
  app, the instance hostname to control, anything else gets `421`.
- Is on its own network with control (and the gateway), never the sandbox network. Control treats it as
  the tunnel caller class: no `/api/agent/*`, no `/api/chat-runtime/*`, never "local".
