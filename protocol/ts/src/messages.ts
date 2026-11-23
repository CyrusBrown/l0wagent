// Envelopes, delivery tokens, fingerprints and friend codes (messages.md). Mirrors protocol/py/src/l0wproto/messages.py.
import { sha256 } from "@noble/hashes/sha2.js";
import * as b64 from "./b64";
import { ENVELOPE_MAX_AGE, MAX_ENVELOPE, MAX_SKEW } from "./consts";
import { type Doc, type Obj, serializePayload, shape, signDocument, verifyDocument } from "./docs";
import { DocumentError, ProtocolError, SealError, SignatureError, ValidationError } from "./errors";
import { Key, type KeyPair } from "./keys";
import * as P from "./payloads";
import { openSealed, seal, sealInfo } from "./seal";
import { now as epochNow, parseTime } from "./times";

/** Validate `body` for `kind`, seal it to `toSealKey`, and sign the envelope. `tokenDoc` must be null for a
 * friend request (and a service-hello) and a delivery-token document otherwise. `ephPriv` fixes the HPKE
 * ephemeral key (vectors only). */
export async function makeEnvelope(
  senderSignKey: KeyPair,
  senderKid: string,
  fromAddr: string,
  toAddr: string,
  toSealKey: Key,
  kind: string,
  body: Obj,
  seq: number,
  tokenDoc: Doc | Obj | null,
  opts: { envId?: string; iat?: string | number; ephPriv?: Uint8Array } = {},
): Promise<Doc> {
  if (senderSignKey.kid !== senderKid) throw new ValidationError("sender_kid is not the signing key's kid");
  if (toSealKey.alg !== "X25519" || toSealKey.use !== "seal") throw new ValidationError("recipient key is not a seal key");
  P.validateBody(kind, body);
  const envId = opts.envId ?? P.newId();
  const info = sealInfo(fromAddr, senderKid, toAddr, toSealKey.kid, envId);
  const sealed = seal(toSealKey.pub, serializePayload(body), info, { ephPriv: opts.ephPriv });
  const payload = P.buildEnvelope(fromAddr, senderKid, toAddr, toSealKey.kid, envId, seq, kind, sealed, tokenDoc, opts.iat);
  P.validateEnvelope(payload);
  const doc = await signDocument("envelope", payload, [senderSignKey]);
  if (b64.utf8(JSON.stringify(doc)).length > MAX_ENVELOPE) throw new ValidationError("envelope is over 16 MiB", "too_large");
  return doc;
}

/** Step 2 of receiving: decode and validate the outer document and payload WITHOUT any signature or time
 * check (a gateway reads `to`, `from`, the token and `kind` from this before step 4). */
export function envelopePayload(doc: unknown): Obj {
  const d = shape(doc);
  if (d.type !== "envelope") throw new DocumentError("not an envelope");
  let p: unknown;
  try {
    p = JSON.parse(b64.fromUtf8(b64.decode(d.payload)));
  } catch (e) {
    if (e instanceof ProtocolError) throw e;
    throw new ValidationError(`payload is not valid JSON: ${(e as Error).message}`);
  }
  if (p !== null && typeof p === "object" && !Array.isArray(p)) {
    const o = p as Obj;
    if (o.v !== d.v || o.type !== d.type) throw new ValidationError("inner v/type differ from outer");
  }
  return P.validateEnvelope(p);
}

/** The `id` of a delivery-token document, or of the token inside an envelope document (null when it carries
 * none). Shape-checked, not signature-checked: a gateway uses it to look the token up in its records. */
export function tokenId(doc: unknown): string | null {
  if (doc !== null && typeof doc === "object" && (doc as Obj).type === "envelope") {
    const tok = envelopePayload(doc).token;
    return tok === undefined || tok === null ? null : tokenId(tok);
  }
  return P.validateTokenDocument(doc).id;
}

/** Make and sign a delivery token: `issuer` (with `issuerKey`) lets `holder` (at `holderKid`) send to it. */
export async function issueDeliveryToken(
  issuerKey: KeyPair,
  issuer: string,
  holder: string,
  holderKid: string,
  opts: { id?: string; iat?: string | number; exp?: string | number } = {},
): Promise<Doc> {
  return signDocument("delivery-token", P.buildDeliveryToken(issuer, issuerKey.kid, holder, holderKid, opts), [issuerKey]);
}

/** The issuer's signature verifies, and the payload's `issuer_kid` is `issuerKey`'s kid. When `holderKid`
 * is given it must equal the token's `holder_kid` (to follow a holder's rotation, pass the kid the token
 * was issued to). Whether the token is one the issuer still remembers is the caller's. */
export async function verifyDeliveryToken(doc: unknown, issuerKey: Key, holderKid?: string | null, now?: number): Promise<Obj> {
  const p = P.validateDeliveryToken(
    await verifyDocument(doc, [issuerKey], { expectedType: "delivery-token", required: [issuerKey.kid], now }),
  );
  if (p.issuer_kid !== issuerKey.kid) throw new SignatureError("token was issued by another key");
  if (holderKid != null && p.holder_kid !== holderKid) throw new SignatureError("token was issued to another key");
  return p;
}

/** Receiving steps 2, 4, 5 (time window) and 6 -> [payload, body]. Token lookup (3), duplicate ids (5) and
 * size/rate (1) are the caller's. `senderSignKey` is the key `from_kid` resolves to (it must carry that kid).
 * Errors carry the protocol code: not_here, bad_signature, bad_document, unknown_kind, bad_seal, bad_body. */
export async function openEnvelope(
  doc: unknown,
  opts: { myAddress: string; mySealKeys: Map<string, Uint8Array> | Record<string, Uint8Array>; senderSignKey: Key; now?: number },
): Promise<[Obj, Obj]> {
  const n = opts.now ?? epochNow();
  const p = envelopePayload(doc);
  const [pn, pd] = P.splitAddress(p.to);
  const [mn, md] = P.splitAddress(opts.myAddress);
  if (pn !== mn || pd !== md) throw new ProtocolError("not this instance's address", "not_here");
  if (opts.senderSignKey.kid !== p.from_kid) throw new SignatureError("from_kid is not the resolved key");
  await verifyDocument(doc, [opts.senderSignKey], { expectedType: "envelope", required: [opts.senderSignKey.kid], now: n });
  const iat = parseTime(p.iat);
  if (iat > n + MAX_SKEW) throw new ValidationError("iat is in the future");
  if (iat < n - ENVELOPE_MAX_AGE) throw new ValidationError("envelope is older than 8 days");
  if (!P.KINDS.includes(p.kind)) throw new ValidationError(`unknown kind ${JSON.stringify(p.kind)}`, "unknown_kind");
  const priv = opts.mySealKeys instanceof Map ? opts.mySealKeys.get(p.to_kid) : Object.hasOwn(opts.mySealKeys, p.to_kid) ? opts.mySealKeys[p.to_kid] : undefined;
  if (priv === undefined) throw new SealError("no key for to_kid");
  const info = sealInfo(p.from, p.from_kid, p.to, p.to_kid, p.id);
  const plain = openSealed(priv, p.sealed, info);
  let body: unknown;
  try {
    body = JSON.parse(b64.fromUtf8(plain));
  } catch {
    throw new ValidationError("body is not valid JSON", "bad_body");
  }
  P.validateBody(p.kind, body);
  return [p, body as Obj];
}

// --- fingerprints and friend codes -------------------------------------------------------------

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const FP = /^[0-9A-HJKMNP-TV-Z]{16}$/;
export const CODE_PREFIX = "l0wf1:";

/** First 10 bytes of SHA-256 of the raw signing public key, Crockford base32, `XXXX-XXXX-XXXX-XXXX`. */
export function fingerprint(pubRaw: Uint8Array): string {
  let n = 0n;
  for (const b of sha256(pubRaw).subarray(0, 10)) n = (n << 8n) | BigInt(b);
  let s = "";
  for (let i = 15; i >= 0; i--) s += CROCKFORD[Number((n >> BigInt(5 * i)) & 31n)];
  return [0, 4, 8, 12].map((i) => s.slice(i, i + 4)).join("-");
}

/** -> [text, secret]. `fingerprint_` may be dashed or not. */
export function makeFriendCode(address: string, fingerprint_: string, secret?: Uint8Array): [string, Uint8Array] {
  const fp = fingerprint_.replaceAll("-", "").toUpperCase();
  if (!FP.test(fp)) throw new ValidationError("bad fingerprint");
  const [name, domain] = P.splitAddress(address);
  const sec = secret ?? crypto.getRandomValues(new Uint8Array(16));
  if (sec.length !== 16) throw new ValidationError("secret must be 16 bytes");
  return [`${CODE_PREFIX}${name}@${domain}:${fp}:${b64.encode(sec)}`, sec];
}

/** -> [address, fingerprint as `XXXX-XXXX-XXXX-XXXX`, secret]. Throws ValidationError (code `bad_friend_code`). */
export function parseFriendCode(text: string): [string, string, Uint8Array] {
  let address: string;
  let fp: string;
  let secret: Uint8Array;
  try {
    if (typeof text !== "string" || !text.startsWith(CODE_PREFIX)) throw new ValidationError("not a friend code");
    const rest = text.slice(CODE_PREFIX.length);
    // the address may itself hold a colon (a port), so split from the right
    const j = rest.lastIndexOf(":");
    const i = j < 0 ? -1 : rest.lastIndexOf(":", j - 1);
    if (j < 0 || i < 0) throw new ValidationError("wrong number of parts");
    address = rest.slice(0, i);
    fp = rest.slice(i + 1, j);
    const sec = rest.slice(j + 1);
    if (address !== address.toLowerCase()) throw new ValidationError("address must be lowercase");
    P.splitAddress(address);
    if (!FP.test(fp)) throw new ValidationError("bad fingerprint");
    secret = b64.decode(sec);
    if (secret.length !== 16) throw new ValidationError("secret must be 16 bytes");
  } catch (e) {
    if (e instanceof ProtocolError) throw new ValidationError(`bad friend code: ${e.detail}`, "bad_friend_code");
    throw e;
  }
  return [address, [0, 4, 8, 12].map((k) => fp.slice(k, k + 4)).join("-"), secret];
}
