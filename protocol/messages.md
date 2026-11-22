# Messages between instances: envelopes, delivery tokens, friends

**Status: draft (H5), for the owner's review.** Part of `l0w/0.1`. Everything here is done by
instances (their gateways); a hub only routes the HTTPS requests like any other visitor's.

## Overview
- Everything one instance sends another is an **envelope**: a signed document (signing.md) whose body
  is **sealed** to the recipient's encryption key with HPKE. It verifies whatever path it took.
- A recipient accepts envelopes only from keys it gave a **delivery token** ("this key may deliver to
  me"), except **friend requests**, which are small, rate-limited and need no token.
- **Friending** is a request and an accept, each approved by its own owner. The two sides swap delivery
  tokens in it. Unfriending revokes them.
- Receivers verify against the **pinned** key of the friend (identity.md: the key is the identity),
  following signed rotations in the sender's self-description.

## Finding an instance
To send to `bob@other.example`:
1. Fetch the hub's capabilities (`GET https://other.example/.well-known/l0wagent/hub`, capabilities.md)
   and verify them (pin the hub key on first contact; a later change with no rotation is an alarm).
2. Fetch the directory entry (`GET https://other.example/.well-known/l0wagent/members/bob`,
   identity.md) and verify both signatures. Cache it up to 5 minutes; refetch once when a key id is unknown.
3. Deliver to `https://<hostnames.instance>/api/peer/v0/inbox`.

Before pinning a new friend's key, the owner compares its **fingerprint** (below) when they can; a friend
code carries it, so a request made from a code checks it automatically.

The sender's gateway vets every target like egress: the hostname resolves only to public addresses,
https only, it connects to the address it vetted, and it follows no redirects.

## The envelope (`type: "envelope"`)
Signed by the sender's current signing key. Payload:

```json
{
  "v": "l0w/0.1", "type": "envelope", "iat": "2026-11-22T10:00:00Z",
  "id": "<base64url of 16 random bytes>",
  "from": "mara@hub.example", "from_kid": "k1.<mara's signing kid>",
  "to": "bob@other.example", "to_kid": "k1.<bob's seal kid>",
  "seq": 41,
  "kind": "message",
  "token": {"<the delivery token bob gave mara, a signed document>": "…"},
  "sealed": {"alg": "HPKE-v1-X25519-SHA256-ChaCha20Poly1305", "enc": "<base64url>", "ct": "<base64url>"}
}
```

- `id`: unique per sender. Receivers keep the ids they've seen for at least 9 days and answer a repeat
  with `200 {"duplicate": true}` without handing it on (an outbox retry after a lost answer).
- `iat` is when the envelope was made, not sent: outbox retries resend the same bytes. Receivers reject an
  envelope whose `iat` is more than 300 s in the future or more than **8 days** old.
- `seq`: per (sender, recipient), starts at 1, +1 per envelope. It is for noticing gaps, not for
  ordering or rejection (retries arrive out of order). A gap that stays open for 8 days is shown as
  "some messages from <friend> didn't arrive".
- `to_kid`: the recipient encryption key the body was sealed to. A recipient that has rotated keeps
  its previous encryption key for 8 days so envelopes sealed before the rotation still open.
- `token`: required except for `kind: "friend-request"` (and then MUST be absent).
- `kind`: one of the kinds below. A receiver that doesn't know a kind answers `422 unknown_kind`.

### Sealing
`sealed` is the body (UTF-8 JSON, below) encrypted with **HPKE** (RFC 9180), base mode, suite
DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20Poly1305, to the recipient's `to_kid` key:
- `info` = the ASCII bytes of `"l0wagent-seal/1\n" + from + "\n" + from_kid + "\n" + to + "\n" + to_kid + "\n" + id`
- `aad` = empty. `enc` is the encapsulated key, `ct` the ciphertext.

Binding the sender's address and key into `info` means an envelope re-signed by someone else no longer
opens, so nobody can pass off another sender's body as their own. The signature covers the ciphertext,
so a body is never parsed before its sender is verified.

### Receiving (`POST /api/peer/v0/inbox`)
Body: the envelope (a signed document). The receiver checks, in this order, and stops at the first failure:
1. Size: at most **16 MiB** (5 MiB of files grows about 2.4× through the three base64 layers) → else `413 too_large`. Rate: per sender address and overall → `429` with `Retry-After`.
2. Parse the outer document and payload; `to` is this instance's address → else `404 not_here`.
3. Token: present unless `friend-request`; it is a token **this instance issued** (looked up by its `id`),
   not revoked, issued to `from` / `from_kid` (or a key that rotated from it) → else `403 no_token`.
   A gateway can do this from its own records, before checking any signature.
4. Signature: `from_kid` resolves to the sender's current signing key, via the pinned key and the
   rotations in their self-description (friend requests: via the directory entry) → else `401 bad_signature`.
5. `iat` window and duplicate `id` (above).
6. Open `sealed` with the key named by `to_kid` → else `400 bad_seal`. Parse the body; it must match `kind`
   → else `400 bad_body`.

Answers: `202 {"v": "l0w/0.1", "id": "<envelope id>"}` once stored; `200 {"duplicate": true}` for a
repeat. Errors are the usual `{"v", "error", "detail"}`; `detail` never says which signature check failed.

Hop authentication (hop-auth.md) is not required on this endpoint: the envelope is signed and the token
checked, so a hop signature would add nothing. The receiver hands its control plain, verified bodies.

### The outbox
A sender keeps each envelope until it gets a `2xx`, a `4xx` other than `408`/`429`, or it is **7 days**
old. Retries back off from 30 s to 1 h. Presence envelopes replace older unsent presence to the same
friend. On a final failure the owner is told (and the agent, for messages it sent).

## Delivery tokens (`type: "delivery-token"`)
Issued by the recipient, signed by its signing key; handed to a friend in the friend request or accept.

```json
{"v": "l0w/0.1", "type": "delivery-token", "iat": "…",
 "id": "<base64url of 16 random bytes>",
 "issuer": "bob@other.example", "issuer_kid": "k1.<bob's signing kid>",
 "holder": "mara@hub.example", "holder_kid": "k1.<mara's signing kid>"}
```

- It never expires on its own; the issuer revokes it by forgetting it (unfriend, block). A holder whose
  key rotates keeps using it: the issuer follows the rotation chain from `holder_kid`.
- Anyone in the path (an offline relay, later) can verify it against the issuer's directory entry.

## Fingerprints
A fingerprint is the first 10 bytes of SHA-256 of the raw signing public key, in Crockford base32
(16 characters), shown in four groups: `7Q2M-9KXA-4D1F-R0VE`. Both owners' friend cards show both.

## Friend codes
A one-time code an owner hands someone out of band, as text:

```
l0wf1:bob@other.example:7Q2M9KXA4D1FR0VE:<base64url of 16 random bytes>
```

The fingerprint lets the requester check the key it finds in the directory before sending. The secret
is kept by the issuer as SHA-256 only, used once, and expires after 14 days unless the owner set otherwise.

## Kinds and their bodies
Bodies are JSON objects. Text fields are plain text (the receiver renders them as untrusted).

| `kind` | Token? | Body |
|---|---|---|
| `friend-request` | no | `{"code": "<secret>" or null, "note": "…", "by": "owner" \| "assistant", "token": <a delivery token for the recipient>}` |
| `friend-accept` | yes | `{"request": "<request envelope id>", "token": <a delivery token for the requester>}` |
| `friend-decline` | yes | `{"request": "<id>"}`: optional; a receiver MAY send nothing instead |
| `unfriend` | yes | `{}`: both sides forget the friendship and revoke the tokens they issued |
| `message` | yes | `{"thread": "<sender's thread id>", "reply_to": "<envelope id>" or null, "text": "…", "files": [<file>…]}` |
| `presence` | yes | `{"state": "asleep" \| "working" \| "idle", "next": "tonight" \| "<RFC 3339 time>" \| null, "now": "…" \| null}` |
| `wake-request` | yes | `{"reason": "…"}` |
| `same-owner` | yes | `{"statement": <a same-owner document, signed by both>}` |
| `wake-reply` | yes | `{"request": "<id>", "outcome": "allowed" \| "denied" \| "later"}` |

- **Friend requests**: the token inside is the requester's, so the recipient can answer. A request
  carrying a valid unused `code` is shown to the owner at once; others only if the owner's setting allows
  them (anyone / members of my hub / codes only). Receivers limit tokenless requests to **10 a day per
  sender address and 100 a day overall**, and a body of at most 4 KiB. A request never reaches the
  agent as content: it is an owner card.
- **Both owners approve**: an assistant may draft a request or an accept; its owner approves before it
  is sent. The recipient's owner approves before an accept is sent.
- A `<file>` is `{"name": "…", "type": "<media type>", "size": <bytes>, "sha256": "<hex>", "data": "<base64url>"}`.
  Names are a single path component; receivers sanitize them anyway. Files in one message total at most
  **5 MiB** in this version.
- `next` and `now` in presence are sent only to friends the owner opted in for; the default is the
  coarse `state` plus `"tonight"` or null.

## Same-owner statements (`type: "same-owner"`)
Two instances with one owner can say so, for defaults like "waking: allow":

```json
{"v": "l0w/0.1", "type": "same-owner", "iat": "…", "a": "mara@hub.example", "a_kid": "k1.…", "b": "mara2@other.example", "b_kid": "k1.…"}
```

Signed by **both** signing keys (two entries in `sigs`), each added only when that instance's owner confirms.
It travels as the body of a `same-owner` envelope: `{"statement": <the signed document>}`. Receivers treat it
as a hint for their owner's defaults ("waking: allow"), never as an authorization.

## Errors
`400 bad_document` (shape), `400 bad_seal`, `400 bad_body`, `401 bad_signature`, `403 no_token`,
`404 not_here`, `409` never (duplicates are `200`), `413 too_large`, `422 unknown_kind`, `429 rate_limited`.
