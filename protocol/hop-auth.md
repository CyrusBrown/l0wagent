# Hop authentication

Authenticates one HTTP request between parties of the protocol ("this request comes from mara's
gateway"), using **HTTP Message Signatures** (RFC 9421) with Ed25519. It says who sent a hop, not
who wrote a message: H5's envelopes are signed separately and verified end to end.

In this version it's used for: instance → hub calls that aren't already signed documents (H3's daily
self-check reads), and calls between gateways that carry no envelope. Envelope delivery (messages.md) doesn't use it: the
envelope is signed and carries a delivery token.

## Signing
- Signature label `l0w`. Covered components, in this order:
  `"@method" "@target-uri" "content-digest" "content-type"` (the last two only when there's a body).
- `Content-Digest: sha-256=:<base64 of SHA-256 of the body>:` (RFC 9530) on every request with a body.
- Parameters: `created` and `expires` (Unix seconds; `expires` at most 300 s after `created`),
  `nonce` (base64url of 16 random bytes), `keyid` (the signer's `kid`), `alg="ed25519"`, and
  `tag="l0w/0.1"`.
- Headers: `Signature-Input` and `Signature`, plus `L0W-Signer: <address>` so the verifier knows
  whose directory entry to fetch.

## Verifying
1. `tag` is a version the verifier speaks; `alg` is `ed25519`; `created` is not more than 300 s in the
   future and `expires` hasn't passed.
2. Resolve `L0W-Signer` to its directory entry (cached up to 5 minutes; refetched once on an unknown
   `keyid`), and `keyid` to a current signing key in it. Revoked keys never verify.
3. Recompute `Content-Digest` from the body received and compare.
4. Check the signature over the signature base (RFC 9421 §2.5).
5. Reject a `(keyid, nonce)` pair seen before within the `expires` window.

Failures answer `401 {"error": "bad_signature"}` with no detail about which step failed.

Test vectors: `vectors/hop-auth.json` (a fixed key, request, signature base and signature).
