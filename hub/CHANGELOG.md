# Changelog

Tags are `hub-X.Y.Z`. Changes to the hub core only; the instance has its own changelog at the repo root.

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
