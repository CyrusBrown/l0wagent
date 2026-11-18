# hub: the generic l0wagent hub core

The hub core for Cloudflare: Workers, Durable Objects and D1. It implements [`protocol/`](../protocol/README.md)
(`l0w/0.1`) and nothing else: **membership** (invites, joining, leaving, re-registration), **identity** (the signed
directory), **capabilities**, **routing** (the tunnel server and the front that sends a member's hostnames down it) and
**basic protection** (rate limit on members' `/api/auth/*`). Hub services (relay, rooms, board, email, an admin page) are
not built here. Nothing in the code names a domain: a hub is its configuration.

It passes the conformance suite (`protocol/conformance/`) running against `wrangler dev`. CI runs that on every push.

```
src/index.ts    the Worker: route by Host (member hostname -> tunnel, else the hub's own API and tunnel socket)
src/api.ts      the core endpoints and the operator's invite endpoint
src/db.ts       D1 queries (conditional updates where something must happen exactly once)
src/member.ts   MemberRoute, a Durable Object per member: the current connection, the /api/auth/* counter
src/tunnel.ts   TunnelConn, a Durable Object per tunnel connection: handshake, frames, flow control
src/config.ts   settings from wrangler vars and secrets
src/proto/      the protocol: base64url, keys, signed documents, payloads, names, invites, tunnel frames, times
migrations/     D1 schema
test/           unit tests; every file in protocol/vectors that applies is reproduced or verified byte for byte
scripts/        invite.mjs (print an invite), conformance.sh (run the suite locally)
```

## Configuration

Everything is a wrangler var (plain) or secret. `wrangler.example.toml` is the production shape, `config.example.md` has
worked examples (two domains, flat, nested), `wrangler.dev.toml` is the local setup the suite uses.

| Setting | Kind | Meaning |
|---|---|---|
| `HUB_NAME` | var | Display name, in the capabilities document and invite lookups |
| `HUB_BASE_URL` | var | `scheme://host[:port]` of the hub's own host. The hub **domain** in members' addresses (`mara@hub.example`) is its host (and port) unless `HUB_DOMAIN` is set |
| `HUB_DOMAIN` | var, optional | Override the domain used in addresses |
| `TUNNEL_URL` | var | The tunnel's WebSocket URL (`wss://…/v0/tunnel`), on the hub's host or another host that routes to this Worker |
| `INSTANCE_HOST_PATTERN` | var | Instance hostname per member, with `{name}` (e.g. `{name}.hub.example`). Filled at join and returned; instances use it as given |
| `FILES_HOST_PATTERN` | var | Files hostname per member (e.g. `{name}.hubfiles.example`). Must be a different origin; two domains is best |
| `RESERVED_NAMES` | var, optional | Extra reserved names, comma separated (on top of the spec's list and the first label of the domain) |
| `MIN_RELEASE` | var, optional | Oldest instance release supported (`instance_releases.min`); default `0.8.0`. Older gets `426` at joining and close `4426` at the tunnel |
| `LIMITS` | var, optional | JSON: `tunnel_streams` (64), `request_body_bytes` (104857600), `auth_rate_per_minute` (20). Missing keys keep defaults |
| `POLICIES` | var, optional | JSON object with `terms` / `privacy` URLs, copied into the capabilities document |
| `OWN_URL` | var, optional | `"true"` (default) or `"false"`: whether members may join with their own URLs |
| `INVITE_DAYS` | var, optional | Default invite lifetime (7) |
| `HUB_KEY_CREATED` | var, optional | The `created` time in the hub's key object (default `2026-11-17T00:00:00Z`); keep it fixed once chosen |
| `HUB_SIGNING_SEED` | **secret** | The hub's Ed25519 key: base64url of 32 random bytes. Changing it changes the hub's identity; instances treat that as an alarm |
| `ADMIN_TOKEN` | **secret** | Bearer token for `POST /v0/admin/invites`. Unset: that endpoint answers 404 |

Make a seed with `node -e 'console.log(require("crypto").randomBytes(32).toString("base64url"))'`.

## Local development

```
cd hub && pnpm install
pnpm typecheck && pnpm test                    # unit tests: the spec's vectors, config, helpers
./scripts/conformance.sh                       # starts wrangler dev, runs the protocol suite, stops it
```

For a hub you can poke at by hand:

```
pnpm exec wrangler d1 migrations apply DB --local --config wrangler.dev.toml
pnpm dev                                       # wrangler dev --local on http://hub.localhost:8787
HUB_ADMIN_TOKEN=dev-admin-token node scripts/invite.mjs --hub http://hub.localhost:8787 --connect 127.0.0.1:8787
```

`*.localhost` names resolve to 127.0.0.1 in browsers; `--connect` does the same for the invite script and the suite.
Under `wrangler dev` an early response to a request whose body hasn't arrived is held until the body is consumed, so the
Worker cancels the body of requests it answers early (the 413 case).

## Deploying

Your real configuration lives in the **private ops repo**: a `wrangler.toml` copied from `wrangler.example.toml`, the
Cloudflare API token, IDs, policies and the deploy workflow. It pins this repo (a `hub-X.Y.Z` tag). Never commit real IDs
or secrets here.

1. **D1**: `wrangler d1 create my-hub`; put the `database_id` in `wrangler.toml`.
2. **Migrations**: `wrangler d1 migrations apply DB --remote` (run it on every release; migrations are only ever added).
3. **Secrets**: `wrangler secret put HUB_SIGNING_SEED` and `wrangler secret put ADMIN_TOKEN`.
4. **Deploy**: `wrangler deploy`. The first deploy creates the Durable Object classes (`MemberRoute`, `TunnelConn`) from the
   `[[migrations]]` block.
5. **Hostnames** (below).
6. **Check**: `curl https://hub.example/.well-known/l0wagent/hub`, then run the suite against it:
   `cd protocol/py && uv run python -m conformance --hub https://hub.example --invite-cmd "node ../../hub/scripts/invite.mjs --hub https://hub.example"`
   with `HUB_ADMIN_TOKEN` set. (It registers about twenty throwaway members; the names stay taken forever, so run it
   against a staging hub, not the live one.)

### Hostnames

Members' hostnames come from the two patterns, **no per-member DNS calls**: production uses a wildcard DNS record and a
wildcard Worker route for each of the two parent domains.

- In each zone, a proxied wildcard record (`*`, `AAAA 100::` is the usual dummy target).
- A Worker route `*.hub.example/*` in the first zone and `*.hubfiles.example/*` in the second (both to this Worker).
- Universal SSL covers the apex and **one** level of subdomain, so `{name}.hub.example` is covered. Flat patterns such as
  `files--{name}.hub.example` need the same wildcard route and the same certificate and nothing more; nested
  `files.{name}.hub.example` would need Advanced Certificate Manager.
- The hub's own host (`hub.example`) is a Workers custom domain; the tunnel can use it (`wss://hub.example/v0/tunnel`).

Don't change a pattern once members have joined: the hub finds a member by matching the Host against the patterns and then
checking the hostname it stored.

### Inviting members

`scripts/invite.mjs` calls `POST /v0/admin/invites` (not part of the spec) with the admin token and prints the invite line:

```
HUB_ADMIN_TOKEN=... node scripts/invite.mjs --hub https://hub.example [--name-hint mara] [--days 7]
HUB_ADMIN_TOKEN=... node scripts/invite.mjs --hub https://hub.example --reregister mara   # a member lost both keys
```

The body is `{name_hint?, days?, reregister?}` and the answer `201 {invite: "l0w-invite:…", expires}`. A re-registration invite
only approves that one name; the hub then issues the signed `reregistration` document (membership.md).

## How the tunnel works here

The WebSocket has to stay in one Durable Object for its whole life, and the member isn't known until the hello arrives. So:

- The Worker sends every upgrade on `/v0/tunnel` to a **`TunnelConn`** Durable Object with a **random id**. That object
  accepts the socket, sends the challenge, reads the member's key from D1, verifies the hello (nonce, hub, `iat`, signature)
  and sends `ready`. Any failure closes `4401` with no detail; a release older than `MIN_RELEASE` closes `4426`.
- It then tells the member's **`MemberRoute`** (a Durable Object named by the member, so one per member) that it is the
  current connection. `MemberRoute` serializes this, so there is exactly one live connection per member: it tells the
  previous `TunnelConn` to drain (no new streams, close `4409` when its open streams end or after 30 s).
- A visitor request for a member hostname goes: Worker (match the Host against the patterns, look the member up in D1,
  with a 10 s per-isolate memory) → `MemberRoute.route()` (left? over the `/api/auth/*` rate limit? which connection?) →
  `TunnelConn.fetch()`, which opens a stream (OPEN, DATA frames within the 262 144-byte window, END) and returns a streaming
  `Response` fed by the instance's frames. The response body is pull-based, so a slow visitor holds the instance back with
  flow control; credit goes out as chunks are consumed.
- The visitor's `CF-Connecting-IP` becomes `client`; `X-Forwarded-For` and anything starting `x-l0w-tunnel-` are never
  believed or forwarded. Hop-by-hop headers are dropped both ways.
- Offline (no connection, or a stream reset before its RESPONSE) answers `502` with `Retry-After: 30`; over `tunnel_streams`
  `503`; a declared or streamed body over `request_body_bytes` `413`; WebSocket upgrades `501`; leaving closes the tunnel
  (close `1000`) and the hostnames answer `410`.
- **Hibernation.** The socket is accepted with the WebSocket Hibernation API, so an idle connection costs no duration:
  the runtime may evict `TunnelConn` and wake it for the next frame or visitor. What the connection needs after a wake
  (member, session id, next stream id, draining, last receive time) is in the socket's attachment. Open streams live
  only in memory, so an interval timer runs exactly while streams are open (timers keep the object awake); it also does
  the hub's PINGs and the 90 s dead rule. While idle, the instance's PING every 30 s wakes the object briefly to answer
  it, and the dead rule is checked when the next visitor arrives (tunnel.md lets the hub's PINGs be optional). The
  handshake's 10 s timer keeps it awake until `ready`.

## Cost and limits to know about

- **Duration billing.** A Durable Object is billed (at 128 MB) for wall-clock time while it is awake, not while it is idle
  and able to hibernate. `TunnelConn` is awake while visitor requests are open (a UI tab's live event stream counts) and for
  a moment every 30 s to answer the instance's PING; an idle member costs a few seconds a day. Before hibernation (0.1.0)
  an always-connected member cost about 10 800 GB-s a day. The allowances: 13 000 GB-s a day on Workers Free (exceeding
  any Free limit makes further operations fail until 00:00 UTC), 400 000 GB-s a month on Paid, then $12.50 per million.
  Watch the duration graph in the dashboard: a member with a browser tab left open all day is awake all day.
- **Requests.** Workers Free allows 100 000 Worker requests and 100 000 Durable Object requests a day. A visitor request
  is one Worker request and two Durable Object requests; incoming WebSocket messages count at 20:1 (about 150 a day per
  idle member for PINGs). That is enough for a few people's personal use; heavy use wants Workers Paid.
- **Request bodies**: Cloudflare caps inbound bodies by plan (100 MB Free/Pro, 200 MB Business, 500 MB Enterprise).
  The default `request_body_bytes` is 100 MiB, above Free/Pro's 100 MB, so Cloudflare answers first there.
- **Subrequests**: a visitor request costs three (D1, `MemberRoute`, `TunnelConn`), well inside 1000 (50 on Free).
- **Per-request lookups**: member lookups are cached per isolate for 10 s; a member that left answers `410` from its
  `MemberRoute` immediately and the cached row only matters for those seconds.
- A visitor that disconnects mid-download: the hub resets the stream (RESET `cancelled`) when the runtime cancels the response
  body or fires the request's abort signal. Under `wrangler dev` only the first is observed, at the next write to the visitor;
  an idle stream with no traffic stays open until the instance next sends, or until the tunnel closes.

## Releases

Tags are `hub-X.Y.Z` (never `v*`: those are instance releases) and never create a GitHub release. Changes go in
[CHANGELOG.md](CHANGELOG.md). The instance's `:edge` image build ignores `hub/**`.
