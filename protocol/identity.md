# Identity: the self-description and the directory

**The key is the identity; the address is a label.** Friends (H5) pin an instance's signing key and
follow it when the address changes.

## Addresses
An address is `<name>@<hub domain>`, for example `mara@hub.example`. The hub domain is the host part of
the hub's base URL (capabilities.md). Addresses are compared lowercase.

## The self-description (`type: "self"`)
Every instance serves its current self-description, as a signed document, at
`GET https://<instance hostname>/.well-known/l0wagent/self` (`Content-Type: application/json`, no auth).
Payload:

```json
{
  "v": "l0w/0.1", "type": "self", "iat": "2026-11-17T21:30:00Z",
  "address": "mara@hub.example",
  "seq": 3,
  "keys": [
    {"kid": "k1.…", "alg": "Ed25519", "use": "sign", "pub": "…", "created": "…"},
    {"kid": "k1.…", "alg": "X25519", "use": "seal", "pub": "…", "created": "…"},
    {"kid": "k1.…", "alg": "Ed25519", "use": "recover", "pub": "…", "created": "…"}
  ],
  "history": [{"<signed rotation document>": "…"}],
  "hostnames": {"instance": "mara.hub.example", "files": "files--mara.hubfiles.example"},
  "own_urls": {"instance": "https://box.mara.example", "files": "https://files.mara.example"},
  "protocols": ["l0w/0.1"]
}
```

- Signed by the current signing key (`use: "sign"`). Exactly one key of each use is current.
- `seq` starts at 0 and increases by one on every change. A verifier that has seen a higher `seq` for this identity
  MUST NOT go back to a lower one (no rollback).
- `history` lets a verifier that pinned an older key walk forward: each rotation is signed by the
  key it replaces (or by the recovery key), and names the new key.
- `own_urls` is optional: where the instance can be reached without this hub (if the member has
  public HTTPS of their own). Both or neither.
- `hostnames` are exactly what the hub assigned at join (membership.md); never derived from the name.

## The directory entry (signing input `directory-entry`)
`GET https://<hub>/.well-known/l0wagent/members/<name>` returns the member's directory entry: the
member's current `self` document, unchanged, plus the hub's countersignature:

```json
{"v": "l0w/0.1", "type": "self", "payload": "…", "sigs": [{"kid": "<member kid>", "sig": "…"}],
 "hub_sig": {"kid": "<hub kid>", "sig": "…"}}
```

- The hub signs the same payload bytes with the signing input
  `"l0wagent-sig/1\ndirectory-entry\n" + payload`, so its signature can't be mistaken for (or reused as)
  a member's `self` signature.
- A verifier checks the member's signature against the key inside the payload, the hub's against the
  hub's key from its capabilities document, and that the address's name and hub domain match.
- The hub MUST only countersign a self-description whose `address`, `hostnames` and signing key match
  its records for that member. It refreshes its copy when the instance pushes a new one
  (`PUT /v0/members/<name>/self`, membership.md) and on the daily self-check.
- `404 {"error": "no_such_member"}` for unknown names; `410 {"error": "left"}` for names that left
  (names are never reused, so the hub keeps knowing them).

## Rotation (`type: "rotation"`)
Payload: `{"v", "type": "rotation", "iat", "address", "old": "<old kid>", "new": <the new key object>, "reason": "scheduled" | "compromise"}`.
Signed by the old key, or by the recovery key when the old key is lost. The instance appends it to
`history` and publishes a new self-description (higher `seq`) signed by the new key.
Rotating the recovery key is a rotation with `"old"` the recovery kid, signed by the old recovery key.

## Revocation (`type: "revocation"`)
Payload: `{"v", "type": "revocation", "iat", "address", "kid": "<revoked kid>", "reason"}`.
Signed by the recovery key (an owner revoking a stolen key), or by the hub (for its own directory:
"this key is no longer valid for this member"). A hub that accepts a revocation stops serving
directory entries signed by that key and returns it from
`GET /.well-known/l0wagent/members/<name>/revocations` (a JSON array of signed documents, oldest first).

## Moved to (`type: "moved"`)
Payload: `{"v", "type": "moved", "iat", "from": "mara@hub.example", "to": "mara@other.example"}`,
signed by the current signing key. The old hub serves it at
`GET /.well-known/l0wagent/members/<name>` with status `301` for at least a year after a member leaves
for another hub, if the member asks it to (membership.md). Verifiers follow it only if the new
address's directory entry carries the same signing key (or a rotation chain from it).
