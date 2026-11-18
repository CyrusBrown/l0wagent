# Keys and signed documents

## Encodings
- **base64url**: RFC 4648 §5, no padding. Decoders MUST reject padding and characters outside the alphabet.
- **Hashes**: SHA-256.

## Keys
A key is a JSON object:

```json
{"kid": "k1.Xr3Qe4w8bLm0aZp1c2d3eQ", "alg": "Ed25519", "use": "sign", "pub": "<base64url of the 32-byte public key>",
 "created": "2026-11-17T21:30:00Z"}
```

- `alg`: `Ed25519` for signing keys (`use: "sign"`), `X25519` for encryption keys (`use: "seal"`).
  Other values MAY appear later; a receiver that doesn't know an `alg` treats the key as unusable, not as an error.
- `kid`: `"k1."` + base64url of the first 16 bytes of SHA-256(`alg` + `"\n"` + raw public key bytes).
  Receivers MUST recompute it and reject a key whose `kid` doesn't match.
- An instance has: one current **signing key**, one current **encryption key**, and a **recovery key**
  (Ed25519, `use: "recover"`), whose private half is shown to the owner once at join and not kept by the
  instance. A hub has one current signing key.

## Signed documents
Every signed thing in the protocol is a **signed document**:

```json
{
  "v": "l0w/0.1",
  "type": "self",
  "payload": "<base64url of the UTF-8 JSON payload bytes>",
  "sigs": [
    {"kid": "k1....", "sig": "<base64url of the 64-byte Ed25519 signature>"}
  ]
}
```

- The payload is carried as **exact bytes** (base64url), so no JSON canonicalization is needed: the
  signer serializes once, and verifiers check the bytes they received, then parse them.
- **Signing input** for each signature: the ASCII bytes of
  `"l0wagent-sig/1\n" + type + "\n" + payload` (where `payload` is the base64url string as it appears).
  The `type` makes a signature for one kind of document useless as another.
- The payload is a JSON object. It MUST contain `"v"` and `"type"` equal to the outer ones, and
  `"iat"` (issued at). It MAY contain `"exp"`. Verifiers MUST reject a document whose inner and outer
  `type` differ, whose `iat` is more than 300 s in the future, or whose `exp` has passed.
- `sigs` holds one or more signatures. Which `kid`s are required depends on the type
  (for example, a directory entry carries the member's signature and the hub's).
- Verifiers MUST reject signatures from a `kid` they can't resolve, and MUST NOT accept a document
  just because *some* signature verified: each required signer is checked.

## Types defined in this version

| `type` | Signed by | Defined in |
|---|---|---|
| `self` | the instance's signing key | identity.md |
| `directory-entry` | the hub (a countersignature on a `self` document, in `hub_sig`) | identity.md |
| `rotation` | the old signing key, or the recovery key | identity.md |
| `revocation` | the recovery key, or the hub (for its own directory) | identity.md |
| `moved` | the instance's signing key | identity.md |
| `hub` | the hub's signing key | capabilities.md |
| `registration` | the instance's new signing key and its recovery key | membership.md |
| `leave` | the instance's signing key | membership.md |
| `reregistration` | the hub | membership.md |
| `tunnel-hello` | the instance's signing key | tunnel.md |

Test vectors for each are in `vectors/`.
