# Accelerators

An **accelerator** is an optional, hub-specific way for a member's visitors to reach the instance
without going through the spec tunnel (tunnel.md). A hub advertises each one in its capabilities
`extras`; no instance needs one, and the spec tunnel stays the baseline: an instance that uses an
accelerator keeps its spec tunnel connected as the fallback.

This version defines one accelerator, **`cloudflared`**, for hubs that run on Cloudflare.

## The `cloudflared` accelerator

The hub gives each member who asks a Cloudflare Tunnel of its own. The member's instance runs
`cloudflared` with the tunnel's token; Cloudflare's edge then sends requests for the member's two
hostnames straight to the instance, and the hub's Worker never sees them. Visitor traffic costs the
hub nothing and is not limited by the spec tunnel's framing (WebSocket upgrades work, for example).

### Advertising

```json
{"kind": "accelerator", "id": "cloudflared", "doc": "https://hub.example/docs/cloudflared",
 "api": "https://hub.example/v0/accelerators/cloudflared"}
```

`api` is the endpoint below. Instances ignore the extra unless they implement it.

### Requests

`POST <api>` with `Content-Type: application/json`; the body is a signed document (signing.md) of type
**`accelerator-request`**, signed by the member's current signing key:

```json
{"v": "l0w/0.1", "type": "accelerator-request", "iat": "2026-11-18T21:30:00Z",
 "address": "mara@hub.example", "hub": "hub.example", "accelerator": "cloudflared",
 "action": "enable", "service": "http://127.0.0.1:8790"}
```

- `hub` is the hub's domain; `accelerator` is `"cloudflared"`.
- `action` is one of:
  - **`enable`**: make (or reuse) the member's tunnel, point it at `service`, and create the DNS
    records for both hostnames. Traffic does not move yet. Idempotent: calling it again returns the
    same tunnel (and updates `service` if it changed). `service` is required and MUST be
    `http://127.0.0.1:<port>` (1–65535); the hub uses it exactly as given and points nothing else at
    the instance.
  - **`activate`**: send the member's hostnames through the tunnel. The hub MAY first check with
    Cloudflare that the tunnel has a live connection and refuse with `409 not_connected` if not.
  - **`deactivate`**: send the hostnames back through the spec tunnel (the tunnel itself stays).
  - **`disable`**: deactivate, then delete the tunnel and its DNS records. Idempotent: disabling a
    member with nothing enabled answers `200` with the disabled state.
- **Freshness**: the hub checks the document as signing.md says, refuses an `iat` more than 120 s in
  the past, and refuses any `iat` not strictly later than the last request it accepted from that member
  (`409 stale`, with `"last_iat"` in the error body). Only accepted (`200`) requests count; the hub keeps
  the last `iat` after `disable` and after the member leaves, so old documents never replay. An instance
  that gets `stale` signs again later.
- Only **active members** may use it.

**Answer** (`200`) for every action:

```json
{"state": {"enabled": true, "active": false, "tunnel_id": "…",
           "hostnames": ["mara.hub.example", "files--mara.hub.example"],
           "token": "<the tunnel's run token>"}}
```

`token` is present only in the answer to `enable`. It is a secret: the instance keeps it in memory or
in its key holder, never in logs. After `disable`, `enabled` and `active` are `false` and `tunnel_id` is
`null`.

**Errors** use the core API's error shape (openapi.json): `400 bad_request` (malformed, unknown action,
bad `service`), `401 bad_sig` (signature, `iat`, `hub`, `accelerator` or address check failed),
`403 not_member`, `409 stale`, `409 not_enabled` (`activate`/`deactivate` before `enable`),
`409 not_connected`, `502 upstream` (the hub's provider failed; `Retry-After` given).

### What the hub does
- **Leaving** (and a re-registration) disables the member's accelerator first.
- While a member is **active**, the hub sees none of their traffic, so the `/api/auth/*` rate limit
  (tunnel.md) does not apply there; the instance limits its own sign-ins, and the hub SHOULD add an
  edge rule for `/api/auth/*`.
- When an `enable` reuses a tunnel, the token may be the same as before; a hub MAY rotate it.
- **Watching active tunnels** (MAY): a member whose instance loses its internet connection can't call
  `deactivate`, and while it is active its visitors get the provider's error instead of the spec
  tunnel's offline answer (tunnel.md). A hub MAY therefore check each active member's tunnel with its
  provider and deactivate it on its own once it has had no live connection for a while; a hub that does
  SHOULD wait at least 60 s first, so a `cloudflared` restart doesn't move the hostnames. The tunnel, its
  token and the DNS records stay; the next `activate` moves the hostnames back.

### What the instance does
- Runs `cloudflared` with the token, its metrics server on loopback, and a listener on the `service`
  address that hands the instance hostname to control and the files hostname to the files app (anything
  else `421`), exactly as the spec tunnel client does. It sets the visitor's address from
  `CF-Connecting-IP` (set by Cloudflare's edge) and drops any incoming header starting `x-l0w-tunnel-`.
- Keeps the spec tunnel connected throughout.
- Calls `activate` once `cloudflared` has been ready for 10 s, and `deactivate` when it has not been
  ready for 30 s while the spec tunnel is connected, so a broken `cloudflared` falls back on its own.
- Doesn't assume it is still active: the hub may have deactivated it (above). When `cloudflared` becomes
  ready again after not being ready, it calls `activate` again once it has been ready for 10 s, and while
  ready it calls `activate` again at least every 15 min. `activate` is idempotent: on an active member
  it changes nothing.
- Calls `disable` when its owner turns the accelerator off.

### Security notes
- A remotely managed Cloudflare Tunnel takes its routing from Cloudflare, so the hub (which holds the API
  token) decides where `cloudflared` forwards. A hub that ignored `service` could point it at anything the
  instance's `cloudflared` can reach. Instances therefore run `cloudflared` where everything reachable is
  either the loopback listener or a service that authenticates its callers (the gateway's tokens; control
  treats everything from the tunnel's network as the tunnel caller class). This is no more trust than the
  hub already has: it terminates TLS for the member's hostnames (decision 8 in the hub plan).
- The tunnel token lets anyone who holds it serve the member's hostnames while they are active. It is
  never written to disk by the instance, and a hub that suspects a leak disables and re-enables (a new
  tunnel, a new token).
