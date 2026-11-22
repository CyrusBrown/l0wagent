# The hub's capabilities

`GET https://<hub>/.well-known/l0wagent/hub` (no auth) returns a signed document of type `hub`.
The hub's **base URL** is the scheme and host this is served from; its domain is the hub domain in
members' addresses. Payload:

```json
{
  "v": "l0w/0.1", "type": "hub", "iat": "2026-11-17T21:30:00Z",
  "name": "Example hub",
  "domain": "hub.example",
  "keys": [{"kid": "k1.…", "alg": "Ed25519", "use": "sign", "pub": "…", "created": "…"}],
  "protocols": ["l0w/0.1"],
  "instance_releases": {"min": "0.8.0"},
  "api": "https://hub.example/v0/",
  "tunnel": "wss://tunnel.hub.example/v0/tunnel",
  "membership": {"invites": true, "signups": "closed", "require_2fa": true},
  "hostnames": {"layout": "two-domains", "example": {"name": "username", "instance": "username.hub.example", "files": "files--username.hubfiles.example"}},
  "policies": {"terms": "https://hub.example/terms", "privacy": "https://hub.example/privacy"},
  "limits": {"tunnel_streams": 64, "request_body_bytes": 104857600, "auth_rate_per_minute": 20},
  "extras": []
}
```

- `keys`: the hub's current signing key, which signs this document; after a rotation the payload also
  has a `history` array of rotation documents (identity.md), so an instance that pinned the old key can follow. Instances pin the hub key at join and treat an unexplained change as
  an alarm (a card for the owner), never something to accept silently.
- `protocols`: versions the hub speaks; `instance_releases.min`: the oldest instance release it still
  supports (versions.md).
- `api`: the base of the hub's core HTTP API (`openapi.json`). `tunnel`: the WebSocket URL (tunnel.md).
- `membership.signups`: `"closed"`, `"open"` or `"approval"`. `require_2fa` is always `true` in this version; instances check it anyway.
  Instances MUST NOT require fields this version doesn't list (older hubs also send `own_url`).
- `hostnames.layout`: informative (`"two-domains"`, `"flat"`, `"nested"`); instances never derive a
  hostname from it. The example shows a new member what theirs will look like: `example.name` (optional)
  is the name it was made for, and hubs SHOULD use `username`. An instance MAY preview a member's
  hostnames by putting the name they typed in place of `example.name` where it stands as a whole part
  of a label (between the start or a `.`/`-` and the end or a `.`/`-`); that is a display, never a
  hostname it uses: the hostnames are the ones registration returns.
- `extras`: optional features, each `{"kind": "...", ...}`. Instances ignore kinds they don't know.
  Defined kinds so far:
  - `{"kind": "accelerator", "id": "cloudflared", "doc": "<url>", "api": "<url>"}`: a faster way to
    connect that a member may use instead of the spec tunnel. No instance needs one. `api` is the
    endpoint instances call (a hub that cannot serve it omits the extra); see [accelerators.md](accelerators.md).
  - H6 adds `{"kind": "service", …}` and H7 `{"kind": "published-hostnames", …}`.

The capabilities document is cacheable (`Cache-Control: max-age=300` is RECOMMENDED). Instances refresh
it at least daily and when the tunnel reconnects.
