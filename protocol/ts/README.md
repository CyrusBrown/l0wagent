# @l0w/proto

The TypeScript reference library of the l0wagent hub protocol (`l0w/0.1`), for hub services that run on
Cloudflare Workers. It mirrors the Python reference library, [`../py/src/l0wproto/`](../py/src/l0wproto/), module for
module and function for function (snake_case there, camelCase here): same names, same checks, same error
codes. The spec is the `.md` files in [`..`](..); the test vectors in [`../vectors/`](../vectors/) are the contract
both libraries are held to.

| Module | Python | What |
|---|---|---|
| `b64`, `consts`, `errors`, `times` | same | base64url (strict), constants and size limits, error classes with protocol codes, RFC 3339 times |
| `keys` | `keys` | `Key`, `KeyPair`, kids, `fromSeed` / `generate` for Ed25519 (WebCrypto) and X25519 (`@noble/curves`) |
| `docs` | `docs` | signed documents: `signDocument`, `verifyDocument`, `verifyOneOf`, directory countersigning |
| `payloads` | `payloads` | builders, validators and typed verifiers for every document type, the envelope body kinds, service and role-grant documents, `servicesIn` |
| `seal` | `seal` | HPKE (RFC 9180) base mode: X25519 / HKDF-SHA256 / ChaCha20Poly1305; `seal`, `openSealed`, `sealInfo` |
| `messages` | `messages` | `makeEnvelope`, `openEnvelope`, delivery tokens, fingerprints, friend codes |
| `names`, `invite`, `tunnel` | same | name rules, invite strings, tunnel frames |

```ts
import { messages, payloads, keys } from "@l0w/proto";

const me = await keys.fromSeed(seed, "sign");
const env = await messages.makeEnvelope(me, me.kid, "svc@hub.example", "mara@hub.example", maraSealKey,
  "service-reply", { call, ok: true, result: {} }, 1, tokenDoc);
const [payload, body] = await messages.openEnvelope(env, { myAddress, mySealKeys: new Map([[kid, priv]]), senderSignKey });
```

Everything that touches a key or a signature is `async` (WebCrypto); validators, `seal` and the fingerprint
are synchronous. Dependencies: `@noble/curves`, `@noble/hashes`, `@noble/ciphers` (pinned exactly; they run on
Workers, and HPKE is written from their primitives so the ephemeral key can be fixed for vectors).

## Not ported
`hopsig` (HTTP Message Signatures between instances, hubs and gateways; hop-auth.md) is not in this library.
It has no TS user yet.

## Tests
```
cd protocol/ts && pnpm install
pnpm typecheck
pnpm test
```
`test/vectors.test.ts` and `test/messages.test.ts` run every vector the Python tests check (byte-exact signing
and sealing with the vectors' fixed ephemeral key); `test/seal.test.ts` checks RFC 9180 A.2.1;
`test/interop.test.ts` has TypeScript seal and Python open and the other way round by running
`uv run python` in `../py` (skipped when `uv` is not installed).

The hub's own copy of the protocol code (`hub/src/proto/`) is separate: `hub/` depends only on `protocol/`
and keeps its own files for now.
