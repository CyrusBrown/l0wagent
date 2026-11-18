# hub/: orientation for coding agents

The generic hub core on Cloudflare (Workers, Durable Objects, D1). It implements `protocol/` and
nothing else. **The repo root's CLAUDE.md describes the instance**: its layout, commands and
security invariants (sandbox network, agent token, egress leases, vault redaction, `/api/agent/*`)
are about control and the agent container and **don't apply here**. What does apply:

- **The spec is the source of truth.** `protocol/*.md`, `protocol/openapi.json` and `protocol/schemas/`.
  If the hub needs a behavior the spec doesn't define, change the spec first (with test vectors for
  anything signed), then the hub. Never edit the conformance suite to make the hub pass.
- **Boundary:** `hub/` depends only on `protocol/` (tests may read its vectors and schemas). It never
  imports instance code (`control/`, `gateway/`, `tunnel/`) or the fake hub (`dev/hub/`), and the
  instance never imports it. `dev/check-boundaries.py` enforces this in CI.
- **Nothing hard-codes a domain.** Every hostname, name, limit and policy comes from configuration
  (wrangler vars and secrets). Your hub's real configuration, IDs and Cloudflare token live in the
  private ops repo, never here; `wrangler.example.toml` shows the shape.
- **Hub services aren't built here** (relay, admin page, rooms, board, email): they go in the
  services repo. This folder is the core: membership, routing, identity, capabilities, basic protection.
- **Done means:** `pnpm typecheck && pnpm test && ./scripts/conformance.sh` pass.
- **Releases:** tags are `hub-X.Y.Z` (never `v*`, which are instance releases), they never create a
  GitHub release (control's update banner reads the latest release), and changes go in
  `hub/CHANGELOG.md`. The instance's `:edge` image build ignores `hub/**`.
- **Commits:** authored by the owner only, no AI co-author lines; commit only when asked.
