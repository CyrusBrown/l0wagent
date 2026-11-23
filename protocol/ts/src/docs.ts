// Signed documents (signing.md) and directory entries (identity.md).
import * as b64 from "./b64";
import { MAX_SKEW, SIG_PREFIX, SUPPORTED_VERSIONS, VERSION } from "./consts";
import { DocumentError, SignatureError, UnsupportedVersion, ValidationError } from "./errors";
import { Key, KeyPair } from "./keys";
import { now as nowEpoch, parseTime } from "./times";

export type Obj = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
export interface SigEntry {
  kid: string;
  sig: string;
}
export interface Doc extends Obj {
  v: string;
  type: string;
  payload: string;
  sigs?: SigEntry[];
  hub_sig?: SigEntry;
}

export const DIRECTORY_TYPE = "directory-entry";
export type KeyMap = Map<string, Key> | Iterable<Key>;

/** `"l0wagent-sig/1\n" + type + "\n" + payload`, as ASCII bytes. */
export function signingInput(type: string, payloadB64: string, prefix = SIG_PREFIX): Uint8Array {
  return b64.utf8(`${prefix}\n${type}\n${payloadB64}`);
}

export function serializePayload(payload: Obj): Uint8Array {
  return b64.utf8(JSON.stringify(payload));
}

function keymap(keys: KeyMap): Map<string, Key> {
  if (keys instanceof Map) return new Map(keys);
  const m = new Map<string, Key>();
  for (const k of keys) m.set(k.kid, k);
  return m;
}

export function isObject(x: unknown): x is Obj {
  return x !== null && typeof x === "object" && !Array.isArray(x);
}

/** Serialize `payload` once and sign it with every signer. */
export async function signDocument(type: string, payload: Obj | Uint8Array, signers: KeyPair[], v = VERSION): Promise<Doc> {
  const raw = payload instanceof Uint8Array ? payload : serializePayload(payload);
  const obj = JSON.parse(b64.fromUtf8(raw));
  if (!isObject(obj) || obj.v !== v || obj.type !== type || !("iat" in obj)) {
    throw new DocumentError("payload must be an object with matching v and type, and iat");
  }
  const p = b64.encode(raw);
  const msg = signingInput(type, p);
  const sigs: SigEntry[] = [];
  for (const s of signers) sigs.push({ kid: s.kid, sig: b64.encode(await s.sign(msg)) });
  return { v, type, payload: p, sigs };
}

export async function checkSig(sig: unknown, keys: Map<string, Key>, msg: Uint8Array): Promise<string> {
  if (!isObject(sig) || typeof sig.kid !== "string" || typeof sig.sig !== "string") {
    throw new SignatureError("malformed signature entry");
  }
  const key = keys.get(sig.kid);
  if (key === undefined || !key.usable) throw new SignatureError("signature from a kid that cannot be resolved");
  const raw = b64.decode(sig.sig);
  if (raw.length !== 64 || !(await key.verify(raw, msg))) throw new SignatureError("signature does not verify");
  return sig.kid;
}

export function shape(doc: unknown): Doc {
  if (!isObject(doc)) throw new DocumentError("document must be an object");
  for (const f of ["v", "type", "payload"]) {
    if (typeof doc[f] !== "string") throw new DocumentError(`document field '${f}' missing`);
  }
  if (!SUPPORTED_VERSIONS.includes(doc.v)) throw new UnsupportedVersion(`unsupported version ${JSON.stringify(doc.v)}`);
  return doc as Doc;
}

/** Decode the payload and apply the inner/outer, iat and exp checks (no signature check). */
export function parsePayload(doc: Doc, now?: number): Obj {
  const n = now ?? nowEpoch();
  let obj: unknown;
  try {
    obj = JSON.parse(b64.fromUtf8(b64.decode(doc.payload)));
  } catch (e) {
    if (e instanceof DocumentError || (e as { code?: string }).code === "bad_encoding") throw e;
    throw new ValidationError(`payload is not valid JSON: ${(e as Error).message}`);
  }
  if (!isObject(obj)) throw new ValidationError("payload must be a JSON object");
  if (obj.v !== doc.v || obj.type !== doc.type) throw new ValidationError("inner v/type differ from outer");
  if (parseTime(obj.iat) > n + MAX_SKEW) throw new ValidationError("iat is in the future");
  if ("exp" in obj && n > parseTime(obj.exp)) throw new ValidationError("document has expired");
  return obj;
}

export interface VerifyOpts {
  expectedType?: string;
  required?: string[];
  now?: number;
}

/** Verify every signature, require each `required` kid (default: every kid present), and return the
 * parsed payload. Any signature that fails or cannot be resolved rejects. */
export async function verifyDocument(doc: unknown, keys: KeyMap, opts: VerifyOpts = {}): Promise<Obj> {
  const d = shape(doc);
  if (opts.expectedType !== undefined && d.type !== opts.expectedType) {
    throw new DocumentError(`expected type '${opts.expectedType}', got '${d.type}'`);
  }
  const sigs = d.sigs;
  if (!Array.isArray(sigs) || sigs.length === 0) throw new SignatureError("no signatures");
  const km = keymap(keys);
  const msg = signingInput(d.type, d.payload);
  const seen: string[] = [];
  for (const s of sigs) seen.push(await checkSig(s, km, msg));
  if (new Set(seen).size !== seen.length) throw new SignatureError("duplicate signer");
  const need = opts.required === undefined ? [...seen] : [...opts.required];
  if (need.length === 0) throw new SignatureError("no required signers");
  const missing = need.filter((k) => !seen.includes(k));
  if (missing.length) throw new SignatureError(`missing required signature from ${missing[0]}`);
  return parsePayload(d, opts.now);
}

/** For types signed by "A or B" (rotation, revocation): exactly one signature, from one of the
 * candidates. Returns [payload, signer kid]. */
export async function verifyOneOf(
  doc: unknown,
  candidates: KeyMap,
  opts: { expectedType?: string; now?: number } = {},
): Promise<[Obj, string]> {
  const km = keymap(candidates);
  const d = shape(doc);
  const sigs = d.sigs;
  if (!Array.isArray(sigs) || sigs.length !== 1 || !isObject(sigs[0])) throw new SignatureError("expected exactly one signature");
  const kid = sigs[0].kid;
  const payload = await verifyDocument(d, km, { expectedType: opts.expectedType, required: [kid], now: opts.now });
  return [payload, kid];
}

// --- directory entries -------------------------------------------------------------------------

/** Add the hub's `hub_sig` over the same payload bytes with type `directory-entry`. */
export async function countersignDirectory(selfDoc: Doc, hub: KeyPair): Promise<Doc> {
  shape(selfDoc);
  if (selfDoc.type !== "self") throw new DocumentError("only a self document can be countersigned");
  const entry: Doc = JSON.parse(JSON.stringify(selfDoc));
  delete entry.hub_sig;
  const msg = signingInput(DIRECTORY_TYPE, selfDoc.payload);
  entry.hub_sig = { kid: hub.kid, sig: b64.encode(await hub.sign(msg)) };
  return entry;
}
