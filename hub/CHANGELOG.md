# Changelog

Tags are `hub-X.Y.Z`. Changes to the hub core only; the instance has its own changelog at the repo root.

## 0.6.0

- Hub services (protocol/services.md): the operator registers a service with `PUT /v0/admin/services/{name}` (and lists or
  removes it with `GET /v0/admin/services` and `DELETE /v0/admin/services/{name}`; same `ADMIN_TOKEN`, 404 when unset).
  The capabilities list each one as `{"kind": "service", "doc": <hub-signed service document>}` in `extras`, its directory
  entry is served countersigned like a member's, and its hostnames answer `404 service_not_here` from the hub (a Cloudflare
  route sends them to the service's own Worker). A service name can't be a member's and a member can't take it.
- Migration `0005_services.sql` (apply it before deploying).

## 0.5.0

- Own-URL mode is removed: members join only through the hub's tunnel (protocol/membership.md). A registration's `mode` and
  `own_urls` and a self description's `own_urls` are ignored if an older instance sends them; `422 own_url_unreachable` and
  the `OWN_URL` setting are gone. Tunnel and accelerator checks now only ask for an active member.
- The capabilities still publish `membership.own_url: false`, only because instances released before this change require the
  field; it can be removed once they are gone.
- No migration: the `members.mode` and `own_urls` columns stay, and `mode` is always written as `tunnel`.

## 0.4.0

- Watching active accelerators (protocol/accelerators.md): a cron trigger runs every minute and asks Cloudflare about each
  active member's tunnel. One with no live connection for `ACCEL_DOWN_AFTER` seconds (default 120) is deactivated, so a
  member whose internet went down shows the hub's offline page instead of Cloudflare's error 1033; its instance activates
  again when `cloudflared` is back (instances from 0.8.0-edge after this change do; older ones on their next restart).
- Migration `0004_accelerator_watch.sql` (apply it before deploying) and `[triggers] crons = ["* * * * *"]` in wrangler.toml.

## 0.3.2

- The capabilities' example hostnames are made for the name `username` and say so (`hostnames.example.name`,
  capabilities.md), so an instance can preview a new member's hostnames. No migration.

## 0.3.1

- Bound invites (protocol/membership.md): `POST /v0/admin/invites` takes `bind: "<name>"`. Registration must use exactly that
  name (`409 name_bound`) and may then take a name the hub reserved itself (its own name, `RESERVED_NAMES`, and their
  lookalikes); the spec's fixed list and taken names stay refused. Creation answers `400 bad_request` for an invalid,
  spec-reserved or taken name, for `bind` with `reregister`, or for a `name_hint` that differs. The invite lookup answers
  `name_bound`. `scripts/invite.mjs --bind NAME`. Migration `0003_bound_invites.sql` (apply it before deploying).

## 0.3.0

- The `cloudflared` accelerator (protocol/accelerators.md): `POST /v0/accelerators/cloudflared` takes a signed
  `accelerator-request` (enable, activate, deactivate, disable) from an active tunnel-mode member and manages a
  Cloudflare Tunnel, its two proxied CNAMEs and two script-less Worker routes through the Cloudflare API. Advertised in the
  capabilities `extras` only when `CF_ACCELERATOR` is `"true"` and the account, zone and token are set. New settings:
  `CF_ACCELERATOR`, `CF_ACCOUNT_ID`, `CF_ZONE_ID`, `CF_FILES_ZONE_ID`, `CF_API_BASE`, `CF_ACCELERATOR_DOC`, secret `CF_API_TOKEN`.
- Migration `0002_accelerator.sql` (apply it before deploying). Leaving and re-registration take the member's accelerator down.
- `scripts/mock-cf-api.mjs`: an in-memory mock of the Cloudflare endpoints used; `conformance.sh` starts it.

## 0.2.0

- The tunnel uses the WebSocket Hibernation API: an idle member's connection costs no Durable Object duration (it was
  about 10 800 GB-s a day per always-connected member, most of the Workers Free allowance). The session survives a wake in
  the socket's attachment; while streams are open a timer keeps the object awake and does the hub's PINGs and the 90 s dead
  rule; while idle the dead rule is checked at the next visitor request. The spec's liveness rule now requires PINGs of the
  instance only (protocol/tunnel.md).

## 0.1.0

First version: the generic hub core on Cloudflare (stage H4), passing the `l0w/0.1` conformance suite against `wrangler dev`.

- Capabilities (`/.well-known/l0wagent/hub`), signed by a key from the `HUB_SIGNING_SEED` secret.
- Invites (`GET /v0/invites/{code}`), registration (`POST /v0/members`: tunnel and own-URL modes, re-registration),
  self-descriptions with rotation chains (`PUT /v0/members/{name}/self`), the signed directory with its revocation and
  re-registration lists, leaving (`POST /v0/members/{name}/leave`, with `moved` documents). Names are never reused;
  lookalikes are refused atomically (a unique column).
- The operator's invite endpoint `POST /v0/admin/invites` and `scripts/invite.mjs`.
- The tunnel server: a Durable Object per connection (random id, so a socket stays in one object) and a `MemberRoute`
  Durable Object per member that holds the current connection, replaces it (4409 after streams drain), rate-limits
  `/api/auth/*` and answers for members that left. Frames, per-stream flow control, streamed bodies both ways,
  PING/PONG and the 90 s dead rule, 4401/4426 closes, 501/502/503/413/429 answers.
- Configuration entirely by wrangler vars and secrets (`wrangler.example.toml`, `config.example.md`); nothing names a domain.
- Unit tests reproduce every applicable file in `protocol/vectors` byte for byte; `scripts/conformance.sh` runs the suite.
