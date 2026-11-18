# Configuration examples

Every setting is a wrangler var or secret (README.md has the table). Real values, IDs and tokens live in
your private ops repo. This file only shows the shapes.

## Two domains (what the owner's hub uses)

Members get `mara.hub.example` (the instance) and `mara.hubfiles.example` (the files app). Two registrable
domains make two *sites*, so agent-made HTML on the files hostname cannot touch the instance's cookies.

```toml
HUB_BASE_URL = "https://hub.example"
TUNNEL_URL = "wss://hub.example/v0/tunnel"
INSTANCE_HOST_PATTERN = "{name}.hub.example"
FILES_HOST_PATTERN = "{name}.hubfiles.example"
```

Wildcard DNS and routes, once per domain (no per-member DNS calls):

| Zone | DNS record (proxied) | Worker route |
|---|---|---|
| `hub.example` | `*` → `AAAA 100::` | `*.hub.example/*` |
| `hubfiles.example` | `*` → `AAAA 100::` | `*.hubfiles.example/*` |

The hub's own host (`hub.example`) is a Workers custom domain (`{ pattern = "hub.example", custom_domain = true }`).
A tunnel host of its own (`wss://tunnel.hub.example/v0/tunnel`) is covered by the same wildcard route.

## Flat

One domain, two hostnames per member: `mara.hub.example` and `files--mara.hub.example`. Names never contain `--`,
so the two patterns cannot collide. The files hostname is the same *site* as the instance (the capabilities
document says `layout: "flat"` and the conformance suite warns); prefer two domains.

```toml
INSTANCE_HOST_PATTERN = "{name}.hub.example"
FILES_HOST_PATTERN = "files--{name}.hub.example"
```

## Nested

`files.mara.hub.example` under `mara.hub.example`: needs a second wildcard level (`*.*.hub.example` is not
covered by Universal SSL; you would need Advanced Certificate Manager). Same site as the instance.

```toml
INSTANCE_HOST_PATTERN = "{name}.hub.example"
FILES_HOST_PATTERN = "files.{name}.hub.example"
```

## Local development

`wrangler.dev.toml` is a complete local configuration on `*.localhost` (browsers resolve those to 127.0.0.1):

```toml
HUB_BASE_URL = "http://hub.localhost:8787"
TUNNEL_URL = "ws://hub.localhost:8787/v0/tunnel"
INSTANCE_HOST_PATTERN = "{name}.hub.localhost:8787"
FILES_HOST_PATTERN = "files--{name}.hubfiles.localhost:8787"
```

The port stays in the hostnames and in the hub's domain (`mara@hub.localhost:8787`), as in the fake hub's
development setup. A real hub has no ports.

## Limits

`LIMITS` is a JSON object; missing keys keep their defaults. The same object is sent to every instance in the
capabilities document and in the tunnel's `ready` message.

```json
{"tunnel_streams": 64, "request_body_bytes": 104857600, "auth_rate_per_minute": 20}
```

Cloudflare's own request body limit depends on your plan (100 MB on Free and Pro, 200 MB Business, 500 MB
Enterprise); a `request_body_bytes` above it is answered by Cloudflare before the hub sees the request.

## The cloudflared accelerator

Members' visitors can skip the Worker. Needs the member zones to be on Cloudflare and an API token (README.md lists its
permissions). Ids are in the dashboard (zone *Overview*, right column).

```toml
CF_ACCELERATOR = "true"
CF_ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
CF_ZONE_ID = "<zone id of hub.example>"
CF_FILES_ZONE_ID = "<zone id of hubfiles.example>"   # omit when both hostnames are in one zone
```

```
wrangler secret put CF_API_TOKEN
```
