// Payload builders, validators and typed verifiers for every document type the hub handles.
// Validators check required fields and types and ignore unknown fields (versions.md). They throw
// ValidationError. The verify* functions combine signature, outer checks and validation.
import { sha256 } from "@noble/hashes/sha2.js";
import * as b64 from "./b64";
import { MAX_FILES, MAX_FRIEND_REQUEST_BODY, VERSION } from "./consts";
import {
  DIRECTORY_TYPE,
  checkSig,
  type Doc,
  isObject,
  type Obj,
  parsePayload,
  shape,
  signingInput,
  verifyDocument,
  verifyOneOf,
} from "./docs";
import { DocumentError, ProtocolError, SignatureError, ValidationError } from "./errors";
import { Key } from "./keys";
import { validateName } from "./names";
import { checkFresh, formatTime, now as epochNow, parseTime } from "./times";

const DOMAIN = /^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$/;

// --- small helpers -----------------------------------------------------------------------------

function needStr(p: Obj, field: string): string {
  if (typeof p[field] !== "string") throw new ValidationError(`field '${field}' missing or not a string`);
  return p[field];
}
function needObj(p: Obj, field: string, what = "an object"): Obj {
  if (!isObject(p[field])) throw new ValidationError(`field '${field}' missing or not ${what}`);
  return p[field];
}
function needList(p: Obj, field: string): unknown[] {
  if (!Array.isArray(p[field])) throw new ValidationError(`field '${field}' missing or not a list`);
  return p[field];
}
function needInt(p: Obj, field: string): number {
  if (!Number.isInteger(p[field])) throw new ValidationError(`field '${field}' missing or not an integer`);
  return p[field];
}
function needBool(p: Obj, field: string): boolean {
  if (typeof p[field] !== "boolean") throw new ValidationError(`field '${field}' missing or not a boolean`);
  return p[field];
}
function strList(p: Obj, field: string): string[] {
  const v = needList(p, field);
  if (!v.every((x) => typeof x === "string")) throw new ValidationError(`field '${field}' must be a list of strings`);
  return v as string[];
}

function base(p: unknown, type: string): Obj {
  if (!isObject(p)) throw new ValidationError("payload must be an object");
  if (p.v !== VERSION || p.type !== type) throw new ValidationError(`payload must have v=${JSON.stringify(VERSION)} and type=${JSON.stringify(type)}`);
  parseTime(p.iat);
  if ("exp" in p) parseTime(p.exp);
  return p;
}

/** `name@domain` -> [name, domain], lowercased and validated. */
export function splitAddress(addr: unknown): [string, string] {
  if (typeof addr !== "string" || addr.split("@").length !== 2) throw new ValidationError("address must be name@domain");
  const [name, domain] = addr.toLowerCase().split("@");
  validateNameSyntax(name);
  if (!DOMAIN.test(domain)) throw new ValidationError("bad hub domain in address");
  return [name, domain];
}

function validateNameSyntax(name: string): void {
  try {
    validateName(name);
  } catch (e) {
    throw new ValidationError(`bad name: ${(e as Error).message}`);
  }
}

export const makeAddress = (name: string, domain: string): string => `${name}@${domain}`.toLowerCase();

async function parseKey(k: unknown): Promise<Key> {
  try {
    return await Key.fromJSON(k);
  } catch (e) {
    if (e instanceof ValidationError) throw e;
    throw new ValidationError(`bad key: ${(e as Error).message}`);
  }
}

async function keysOf(p: Obj, field = "keys"): Promise<Key[]> {
  const raw = needList(p, field);
  const out: Key[] = [];
  for (const k of raw) out.push(await parseKey(k));
  return out;
}

function onePerUse(keys: Key[], uses: string[]): Record<string, Key> {
  const out: Record<string, Key> = {};
  for (const u of uses) {
    const found = keys.filter((k) => k.use === u && k.usable);
    if (found.length !== 1) throw new ValidationError(`exactly one '${u}' key required, found ${found.length}`);
    out[u] = found[0];
  }
  return out;
}

function urlPair(v: unknown, field: string): void {
  if (!isObject(v) || typeof v.instance !== "string" || typeof v.files !== "string") {
    throw new ValidationError(`'${field}' must have string 'instance' and 'files'`);
  }
}

/** Builders put `v`, `type` and `iat` first, then the fields in order; `undefined` fields are left out. */
function pl(type: string, iat: string | number | undefined, exp: string | number | undefined, fields: Obj): Obj {
  const out: Obj = {
    v: VERSION,
    type,
    iat: typeof iat === "string" ? iat : formatTime(iat ?? epochNow()),
  };
  if (exp !== undefined) out.exp = typeof exp === "string" ? exp : formatTime(exp);
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) out[k] = v;
  return out;
}

const kd = (keys: Array<Key | Obj>): Obj[] => keys.map((k) => (k instanceof Key ? k.toJSON() : k));

export function shapeOk(doc: unknown): Doc {
  if (!isObject(doc) || typeof doc.payload !== "string" || typeof doc.type !== "string") {
    throw new DocumentError("document must have type and payload");
  }
  return shape(doc);
}

// --- self --------------------------------------------------------------------------------------

export function buildSelf(
  address: string,
  seq: number,
  keys: Array<Key | Obj>,
  hostnames: Obj,
  opts: { protocols?: string[]; history?: Obj[]; iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("self", opts.iat, opts.exp, {
    address,
    seq,
    keys: kd(keys),
    history: [...(opts.history ?? [])],
    hostnames,
    protocols: opts.protocols ?? [VERSION],
  });
}

export async function validateSelf(p: unknown): Promise<Obj> {
  const o = base(p, "self");
  splitAddress(needStr(o, "address"));
  if (needInt(o, "seq") < 0) throw new ValidationError("seq must not be negative");
  onePerUse(await keysOf(o), ["sign", "seal", "recover"]);
  for (const d of needList(o, "history")) {
    if (!isObject(d) || d.type !== "rotation") throw new ValidationError("history entries must be signed rotation documents");
  }
  urlPair(o.hostnames, "hostnames");
  strList(o, "protocols");
  return o;
}

export async function selfSigningKey(p: Obj): Promise<Key> {
  return onePerUse(await keysOf(p), ["sign"]).sign;
}

/** Verify a self document (or a directory entry's member half) against the key inside it. */
export async function verifySelf(doc: unknown, now?: number): Promise<Obj> {
  const d = shapeOk(doc);
  const p = await validateSelf(parsePayload(d, now));
  const k = await selfSigningKey(p);
  if ((Array.isArray(d.sigs) ? d.sigs.length : 0) !== 1) throw new SignatureError("a self document carries exactly one signature");
  return validateSelf(await verifyDocument(d, [k], { expectedType: "self", required: [k.kid], now }));
}

export async function verifyDirectoryEntry(
  entry: Doc,
  hubKey: Key,
  opts: { hubDomain: string; name?: string; now?: number },
): Promise<Obj> {
  const payload = await verifySelf(entry, opts.now);
  const hs = entry.hub_sig;
  if (!isObject(hs) || hs.kid !== hubKey.kid) throw new SignatureError("hub_sig missing or from another key");
  await checkSig(hs, new Map([[hubKey.kid, hubKey]]), signingInput(DIRECTORY_TYPE, entry.payload));
  const [name, domain] = splitAddress(payload.address);
  if (domain !== opts.hubDomain.toLowerCase()) throw new ValidationError("address is on another hub");
  if (opts.name !== undefined && name !== opts.name.toLowerCase()) throw new ValidationError("address does not match the requested name");
  return payload;
}

// --- rotation ----------------------------------------------------------------------------------

export function buildRotation(
  address: string,
  oldKid: string,
  newKey: Key | Obj,
  reason = "scheduled",
  opts: { iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("rotation", opts.iat, opts.exp, { address, old: oldKid, new: newKey instanceof Key ? newKey.toJSON() : newKey, reason });
}

export async function validateRotation(p: unknown): Promise<Obj> {
  const o = base(p, "rotation");
  splitAddress(needStr(o, "address"));
  needStr(o, "old");
  needObj(o, "new", "a key object");
  try {
    await Key.fromJSON(o.new);
  } catch (e) {
    throw new ValidationError(`bad new key: ${(e as Error).message}`);
  }
  if (o.reason !== "scheduled" && o.reason !== "compromise") throw new ValidationError("reason must be 'scheduled' or 'compromise'");
  return o;
}

/** Signed by the key it replaces, or by the recovery key (when the old key is lost). */
export async function verifyRotation(doc: unknown, oldKey: Key, recoveryKey?: Key | null, now?: number): Promise<Obj> {
  const cands = [oldKey, ...(recoveryKey ? [recoveryKey] : [])];
  const [p] = await verifyOneOf(doc, cands, { expectedType: "rotation", now });
  await validateRotation(p);
  if (p.old !== oldKey.kid) throw new ValidationError("rotation does not replace the expected key");
  return p;
}

// --- revocation --------------------------------------------------------------------------------

export function buildRevocation(address: string, kid: string, reason: string, iat?: string | number): Obj {
  return pl("revocation", iat, undefined, { address, kid, reason });
}

export function validateRevocation(p: unknown): Obj {
  const o = base(p, "revocation");
  splitAddress(needStr(o, "address"));
  needStr(o, "kid");
  needStr(o, "reason");
  return o;
}

/** Signed by the member's recovery key, or by the hub (for its own directory). */
export async function verifyRevocation(doc: unknown, allowed: Key[], now?: number): Promise<Obj> {
  const [p] = await verifyOneOf(doc, allowed, { expectedType: "revocation", now });
  return validateRevocation(p);
}

// --- moved -------------------------------------------------------------------------------------

export function buildMoved(fromAddr: string, toAddr: string, iat?: string | number, exp?: string | number): Obj {
  return pl("moved", iat, exp, { from: fromAddr, to: toAddr });
}

export function validateMoved(p: unknown): Obj {
  const o = base(p, "moved");
  splitAddress(needStr(o, "from"));
  splitAddress(needStr(o, "to"));
  return o;
}

export async function verifyMoved(doc: unknown, signingKey: Key, now?: number): Promise<Obj> {
  return validateMoved(await verifyDocument(doc, [signingKey], { expectedType: "moved", required: [signingKey.kid], now }));
}

// --- hub ---------------------------------------------------------------------------------------

export interface HubBuild {
  name: string;
  domain: string;
  keys: Array<Key | Obj>;
  api: string;
  tunnel: string;
  protocols?: string[];
  minRelease: string;
  membership: Obj;
  hostnames: Obj;
  policies?: Obj;
  limits: Obj;
  extras?: Obj[];
  iat?: string | number;
}

export function buildHub(h: HubBuild): Obj {
  return pl("hub", h.iat, undefined, {
    name: h.name,
    domain: h.domain,
    keys: kd(h.keys),
    protocols: h.protocols ?? [VERSION],
    instance_releases: { min: h.minRelease },
    api: h.api,
    tunnel: h.tunnel,
    membership: h.membership,
    hostnames: h.hostnames,
    policies: h.policies,
    limits: h.limits,
    extras: h.extras ?? [],
  });
}

export async function validateHub(p: unknown): Promise<Obj> {
  const o = base(p, "hub");
  needStr(o, "name");
  if (!DOMAIN.test(needStr(o, "domain"))) throw new ValidationError("bad hub domain");
  const ks = await keysOf(o);
  if (!ks.some((k) => k.use === "sign" && k.alg === "Ed25519")) throw new ValidationError("hub needs an Ed25519 signing key");
  strList(o, "protocols");
  if (typeof needObj(o, "instance_releases").min !== "string") throw new ValidationError("field 'min' missing or not a string");
  if (!needStr(o, "api").endsWith("/")) throw new ValidationError("api must end with a slash");
  needStr(o, "tunnel");
  const m = needObj(o, "membership");
  if (!["closed", "open", "approval"].includes(m.signups)) throw new ValidationError("membership.signups must be closed, open or approval");
  for (const f of ["invites", "require_2fa"]) needBool(m, f);
  needObj(o, "hostnames");
  const lim = needObj(o, "limits");
  for (const f of ["tunnel_streams", "request_body_bytes", "auth_rate_per_minute"]) needInt(lim, f);
  for (const e of (o.extras ?? []) as unknown[]) {
    if (!isObject(e) || typeof e.kind !== "string") throw new ValidationError("extras entries need a 'kind'");
  }
  return o;
}

/** Verify a capabilities document (used by tests; the hub signs its own). */
export async function verifyHub(doc: unknown, pinned?: Key, now?: number): Promise<Obj> {
  const d = shapeOk(doc);
  const p = await validateHub(parsePayload(d, now));
  const ks = (await keysOf(p)).filter((k) => k.use === "sign" && k.alg === "Ed25519");
  if ((Array.isArray(d.sigs) ? d.sigs.length : 0) !== 1) throw new SignatureError("a hub document carries exactly one signature");
  const v = await validateHub(await verifyDocument(d, ks, { expectedType: "hub", now }));
  if (pinned && !(await keysOf(v)).some((k) => k.kid === pinned.kid)) throw new SignatureError("hub key does not match the pinned key");
  if (v.membership.require_2fa !== true) throw new ValidationError("hub does not require 2FA");
  return v;
}

// --- registration ------------------------------------------------------------------------------

export function buildRegistration(
  hub: string,
  name: string,
  invite: string,
  keys: Array<Key | Obj>,
  opts: { release?: string; protocols?: string[]; reregister?: boolean; iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("registration", opts.iat, opts.exp, {
    hub,
    name,
    invite,
    keys: kd(keys),
    instance: { release: opts.release ?? "0.9.0", protocols: opts.protocols ?? [VERSION] },
    reregister: opts.reregister,
  });
}

export async function validateRegistration(p: unknown, hubDomain?: string, now?: number): Promise<Obj> {
  const o = base(p, "registration");
  checkFresh(o.iat, now);
  const hub = needStr(o, "hub");
  if (hubDomain !== undefined && hub.toLowerCase() !== hubDomain.toLowerCase()) throw new ValidationError("registration is for another hub");
  validateNameSyntax(needStr(o, "name"));
  needStr(o, "invite");
  onePerUse(await keysOf(o), ["sign", "seal", "recover"]);
  const inst = needObj(o, "instance");
  needStr(inst, "release");
  strList(inst, "protocols");
  if ("reregister" in o) needBool(o, "reregister");
  return o;
}

/** Signed by BOTH the new signing key and the recovery key listed in the payload. */
export async function verifyRegistration(doc: unknown, hubDomain: string, now?: number): Promise<Obj> {
  const d = shapeOk(doc);
  const p = await validateRegistration(parsePayload(d, now), hubDomain, now);
  const ks = onePerUse(await keysOf(p), ["sign", "recover"]);
  const req = [ks.sign.kid, ks.recover.kid];
  if ((Array.isArray(d.sigs) ? d.sigs.length : 0) !== 2) throw new SignatureError("a registration carries exactly two signatures");
  await verifyDocument(d, Object.values(ks), { expectedType: "registration", required: req, now });
  return p;
}

// --- leave -------------------------------------------------------------------------------------

export function buildLeave(address: string, movedTo: string | null = null, iat?: string | number, exp?: string | number): Obj {
  return pl("leave", iat, exp, { address, moved_to: movedTo });
}

export function validateLeave(p: unknown): Obj {
  const o = base(p, "leave");
  splitAddress(needStr(o, "address"));
  if (!("moved_to" in o)) throw new ValidationError("moved_to is required (null if none)");
  if (o.moved_to !== null) splitAddress(needStr(o, "moved_to"));
  return o;
}

export async function verifyLeave(doc: unknown, signingKey: Key, now?: number): Promise<Obj> {
  return validateLeave(await verifyDocument(doc, [signingKey], { expectedType: "leave", required: [signingKey.kid], now }));
}

// --- reregistration ----------------------------------------------------------------------------

export function buildReregistration(address: string, oldKeys: string[], newKeys: string[], iat?: string | number): Obj {
  return pl("reregistration", iat, undefined, { address, old_keys: [...oldKeys], new_keys: [...newKeys] });
}

export function validateReregistration(p: unknown): Obj {
  const o = base(p, "reregistration");
  splitAddress(needStr(o, "address"));
  strList(o, "old_keys");
  strList(o, "new_keys");
  return o;
}

export async function verifyReregistration(doc: unknown, hubKey: Key, now?: number): Promise<Obj> {
  return validateReregistration(await verifyDocument(doc, [hubKey], { expectedType: "reregistration", required: [hubKey.kid], now }));
}

// --- tunnel-hello ------------------------------------------------------------------------------

export function buildTunnelHello(
  address: string,
  nonce: string,
  hub: string,
  release = "0.9.0",
  opts: { iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("tunnel-hello", opts.iat, opts.exp, { address, nonce, hub, release });
}

export function validateTunnelHello(p: unknown): Obj {
  const o = base(p, "tunnel-hello");
  splitAddress(needStr(o, "address"));
  if (b64.decode(needStr(o, "nonce")).length !== 32) throw new ValidationError("nonce must be 32 bytes");
  needStr(o, "hub");
  needStr(o, "release");
  return o;
}

/** The hub's check at step 4 of the handshake (nonce single-use/30 s age is the hub's own state). */
export async function verifyTunnelHello(doc: unknown, signingKey: Key, opts: { nonce: string; hub: string; now?: number }): Promise<Obj> {
  const p = validateTunnelHello(
    await verifyDocument(doc, [signingKey], { expectedType: "tunnel-hello", required: [signingKey.kid], now: opts.now }),
  );
  if (p.nonce !== opts.nonce) throw new ValidationError("nonce mismatch");
  if (p.hub.toLowerCase() !== opts.hub.toLowerCase()) throw new ValidationError("hello is for another hub");
  checkFresh(p.iat, opts.now);
  return p;
}

// --- accelerator-request -----------------------------------------------------------------------

export const ACCELERATOR_ACTIONS = ["enable", "activate", "deactivate", "disable"] as const;
export const ACCELERATOR_MAX_AGE = 120; // seconds an accelerator-request's iat may lie in the past
const SERVICE = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/;

export function validService(service: unknown): boolean {
  const m = typeof service === "string" ? SERVICE.exec(service) : null;
  return m !== null && Number(m[1]) <= 65535;
}

export function buildAcceleratorRequest(
  address: string,
  hub: string,
  action: string,
  service?: string,
  opts: { accelerator?: string; iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("accelerator-request", opts.iat, opts.exp, { address, hub, accelerator: opts.accelerator ?? "cloudflared", action, service });
}

/** Shape rules; failures are `bad_request`. */
export function validateAcceleratorRequest(p: unknown): Obj {
  const o = base(p, "accelerator-request");
  splitAddress(needStr(o, "address"));
  needStr(o, "hub");
  needStr(o, "accelerator");
  const action = needStr(o, "action");
  if (!(ACCELERATOR_ACTIONS as readonly string[]).includes(action)) throw new ValidationError(`unknown action ${JSON.stringify(action)}`);
  if (action === "enable" && !validService(o.service)) {
    throw new ValidationError("service must be http://127.0.0.1:<port> with a port from 1 to 65535");
  }
  return o;
}

/** The hub's check. Throws a ProtocolError whose `code` is `bad_sig` (signature, iat, hub, accelerator or
 * address) or `bad_request` (malformed, action, service). Strictly increasing `iat` is the hub's own state. */
export async function verifyAcceleratorRequest(
  doc: unknown,
  signingKey: Key,
  opts: { hub: string; address?: string; accelerator?: string; now?: number },
): Promise<Obj> {
  const n = opts.now ?? epochNow();
  let p: Obj;
  try {
    p = await verifyDocument(doc, [signingKey], { expectedType: "accelerator-request", required: [signingKey.kid], now: n });
  } catch (e) {
    if (e instanceof SignatureError) throw new SignatureError(e.detail, "bad_sig");
    if (e instanceof ValidationError && /iat|expired/.test(e.detail)) throw new SignatureError(e.detail, "bad_sig");
    if (e instanceof ValidationError || e instanceof DocumentError) throw new ValidationError(e.detail, "bad_request");
    throw e;
  }
  try {
    validateAcceleratorRequest(p);
  } catch (e) {
    if (e instanceof ValidationError) throw new ValidationError(e.detail, "bad_request");
    throw e;
  }
  if (parseTime(p.iat) < n - ACCELERATOR_MAX_AGE) throw new SignatureError("iat is more than 120 s in the past", "bad_sig");
  if (p.hub.toLowerCase() !== opts.hub.toLowerCase()) throw new SignatureError("request is for another hub", "bad_sig");
  if (p.accelerator !== (opts.accelerator ?? "cloudflared")) throw new SignatureError("request is for another accelerator", "bad_sig");
  if (opts.address !== undefined && p.address.toLowerCase() !== opts.address.toLowerCase()) {
    throw new SignatureError("request is for another address", "bad_sig");
  }
  return p;
}

// --- envelope, delivery-token, same-owner (messages.md) ----------------------------------------

export const SERVICE_KINDS = ["service-hello", "service-welcome", "service-bye", "service-call", "service-reply", "service-event"] as const;
export const KINDS: readonly string[] = [
  "friend-request",
  "friend-accept",
  "friend-decline",
  "unfriend",
  "message",
  "presence",
  "wake-request",
  "same-owner",
  "wake-reply",
  ...SERVICE_KINDS,
];
export const TOKENLESS_KINDS: readonly string[] = ["friend-request", "service-hello"]; // envelope kinds that carry no delivery token
export const PRESENCE_STATES: readonly string[] = ["asleep", "working", "idle"];
export const WAKE_OUTCOMES: readonly string[] = ["allowed", "denied", "later"];
export const BY_VALUES: readonly string[] = ["owner", "assistant"];

/** 16 random bytes, base64url (envelope and token ids). */
export function newId(): string {
  return b64.encode(crypto.getRandomValues(new Uint8Array(16)));
}

function idField(p: Obj, field = "id"): string {
  const v = needStr(p, field);
  let ok: boolean;
  try {
    ok = b64.decode(v).length === 16;
  } catch {
    ok = false;
  }
  if (!ok) throw new ValidationError(`field '${field}' must be the base64url of 16 bytes`);
  return v;
}

const KID = /^k1\.[A-Za-z0-9_-]{22}$/;
function kidField(p: Obj, field: string): string {
  const v = needStr(p, field);
  if (!KID.test(v)) throw new ValidationError(`field '${field}' is not a key id`);
  return v;
}

export function buildDeliveryToken(
  issuer: string,
  issuerKid: string,
  holder: string,
  holderKid: string,
  opts: { id?: string; iat?: string | number; exp?: string | number } = {},
): Obj {
  return pl("delivery-token", opts.iat, opts.exp, { id: opts.id ?? newId(), issuer, issuer_kid: issuerKid, holder, holder_kid: holderKid });
}

export function validateDeliveryToken(p: unknown): Obj {
  const o = base(p, "delivery-token");
  idField(o);
  splitAddress(needStr(o, "issuer"));
  splitAddress(needStr(o, "holder"));
  kidField(o, "issuer_kid");
  kidField(o, "holder_kid");
  return o;
}

export function buildSameOwner(a: string, aKid: string, b: string, bKid: string, iat?: string | number, exp?: string | number): Obj {
  return pl("same-owner", iat, exp, { a, a_kid: aKid, b, b_kid: bKid });
}

export function validateSameOwner(p: unknown): Obj {
  const o = base(p, "same-owner");
  const a = splitAddress(needStr(o, "a"));
  const b = splitAddress(needStr(o, "b"));
  const ka = kidField(o, "a_kid");
  const kb = kidField(o, "b_kid");
  if ((a[0] === b[0] && a[1] === b[1]) || ka === kb) {
    throw new ValidationError("a same-owner statement needs two different instances and keys");
  }
  return o;
}

/** Both signatures must verify, each against the key named in the payload (`a_kid`, `b_kid`). */
export async function verifySameOwner(doc: unknown, keyA: Key, keyB: Key, now?: number): Promise<Obj> {
  const p = validateSameOwner(await verifyDocument(doc, [keyA, keyB], { expectedType: "same-owner", required: [keyA.kid, keyB.kid], now }));
  if (p.a_kid !== keyA.kid || p.b_kid !== keyB.kid) throw new SignatureError("keys do not match the statement");
  return p;
}

/** The envelope payload. `token` (a signed delivery-token document) goes in unless it is null/undefined. */
export function buildEnvelope(
  fromAddr: string,
  fromKid: string,
  toAddr: string,
  toKid: string,
  envId: string,
  seq: number,
  kind: string,
  sealed: Obj,
  token: Obj | null = null,
  iat?: string | number,
): Obj {
  return pl("envelope", iat, undefined, {
    id: envId,
    from: fromAddr,
    from_kid: fromKid,
    to: toAddr,
    to_kid: toKid,
    seq,
    kind,
    token: token ?? undefined,
    sealed,
  });
}

/** Shape rules. An unknown `kind` is not a shape error (the receiver answers 422 unknown_kind). */
export function validateEnvelope(p: unknown): Obj {
  const o = base(p, "envelope");
  idField(o);
  splitAddress(needStr(o, "from"));
  splitAddress(needStr(o, "to"));
  kidField(o, "from_kid");
  kidField(o, "to_kid");
  if (needInt(o, "seq") < 1) throw new ValidationError("seq starts at 1");
  const kind = needStr(o, "kind");
  if (TOKENLESS_KINDS.includes(kind)) {
    if ("token" in o) throw new ValidationError(`a ${kind} carries no token`);
  } else {
    if (!isObject(o.token)) throw new ValidationError("token required");
    validateTokenDocument(o.token);
  }
  const s = needObj(o, "sealed");
  for (const f of ["alg", "enc", "ct"]) needStr(s, f);
  return o;
}

function innerOf(doc: unknown, type: string, what: string): unknown {
  if (!isObject(doc) || doc.type !== type || doc.v !== VERSION || typeof doc.payload !== "string" || !Array.isArray(doc.sigs)) {
    throw new ValidationError(`not a ${what} document`);
  }
  try {
    return JSON.parse(b64.fromUtf8(b64.decode(doc.payload)));
  } catch {
    throw new ValidationError(`${what} payload is not valid`);
  }
}

/** A delivery-token as a signed document: shape and payload shape only (no signature check). */
export function validateTokenDocument(doc: unknown): Obj {
  return validateDeliveryToken(innerOf(doc, "delivery-token", "delivery-token"));
}

export function validateStatementDocument(doc: unknown): Obj {
  return validateSameOwner(innerOf(doc, "same-owner", "same-owner"));
}

export function validateFile(f: unknown): Obj {
  if (!isObject(f)) throw new ValidationError("a file must be an object");
  const name = needStr(f, "name");
  if (!name || name.includes("/") || name.includes("\\") || name.includes("\x00") || name === "." || name === "..") {
    throw new ValidationError("file name must be a single path component");
  }
  needStr(f, "type");
  const size = needInt(f, "size");
  const sha = needStr(f, "sha256");
  if (size < 0 || !/^[0-9a-f]{64}$/.test(sha)) throw new ValidationError("bad file size or sha256");
  let raw: Uint8Array;
  try {
    raw = b64.decode(needStr(f, "data"));
  } catch {
    throw new ValidationError("file data is not base64url");
  }
  if (raw.length !== size) throw new ValidationError("file size does not match its data");
  if (b64.toHex(sha256(raw)) !== sha) throw new ValidationError("file sha256 does not match its data");
  return f;
}

export function buildFile(name: string, mediaType: string, data: Uint8Array): Obj {
  return { name, type: mediaType, size: data.length, sha256: b64.toHex(sha256(data)), data: b64.encode(data) };
}

function optStr(b: Obj, field: string): void {
  if (b[field] !== undefined && b[field] !== null && typeof b[field] !== "string") {
    throw new ValidationError(`field '${field}' must be a string or null`);
  }
}

const jsonBytes = (o: unknown): number => b64.utf8(JSON.stringify(o)).length;

/** The body rules per kind. Throws ValidationError with code `bad_body`; unknown kinds throw code `unknown_kind`. */
export function validateBody(kind: string, body: unknown): Obj {
  try {
    return validateBodyInner(kind, body);
  } catch (e) {
    if (e instanceof ValidationError && e.code === "bad_document") e.code = "bad_body";
    throw e;
  }
}

function validateBodyInner(kind: string, b: unknown): Obj {
  if (!KINDS.includes(kind)) throw new ValidationError(`unknown kind ${JSON.stringify(kind)}`, "unknown_kind");
  if (!isObject(b)) throw new ValidationError("body must be an object");
  if (kind === "friend-request") {
    optStr(b, "code");
    if (!("code" in b)) throw new ValidationError("field 'code' missing (use null)");
    needStr(b, "note");
    if (!BY_VALUES.includes(needStr(b, "by"))) throw new ValidationError("'by' must be owner or assistant");
    validateTokenDocument(b.token);
    if (jsonBytes(b) > MAX_FRIEND_REQUEST_BODY) throw new ValidationError("friend request body is over 4 KiB");
  } else if (kind === "friend-accept") {
    needStr(b, "request");
    validateTokenDocument(b.token);
  } else if (kind === "friend-decline") {
    needStr(b, "request");
  } else if (kind === "message") {
    needStr(b, "thread");
    optStr(b, "reply_to");
    needStr(b, "text");
    let total = 0;
    for (const f of needList(b, "files")) {
      validateFile(f);
      total += (f as Obj).size;
    }
    if (total > MAX_FILES) throw new ValidationError("files total more than 5 MiB");
  } else if (kind === "presence") {
    if (!PRESENCE_STATES.includes(needStr(b, "state"))) throw new ValidationError("bad presence state");
    const nxt = b.next;
    if (nxt !== undefined && nxt !== null && nxt !== "tonight") parseTime(nxt);
    optStr(b, "now");
  } else if (kind === "wake-request") {
    needStr(b, "reason");
  } else if (kind === "same-owner") {
    validateStatementDocument(b.statement);
  } else if (kind === "wake-reply") {
    needStr(b, "request");
    if (!WAKE_OUTCOMES.includes(needStr(b, "outcome"))) throw new ValidationError("bad outcome");
  } else if ((SERVICE_KINDS as readonly string[]).includes(kind)) {
    validateServiceBody(kind, b);
  }
  return b; // unfriend, service-bye: any object
}

// --- hub services (services.md) ----------------------------------------------------------------

export const SERVICE_WELL_KNOWN = ["relay", "rooms", "board", "email", "spaces", "directory", "admin"] as const;
export const SERVICE_DEFAULTS = ["on", "off"] as const;
export const MAX_SERVICE_BODY = 1024 * 1024; // bytes, the serialized body of a service-call or service-reply
const ROLE = /^[a-z][a-z0-9_-]{0,31}$/;
const HEX_COMMIT = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const HTTPS_URL = /^https:\/\/[^\s/?#][^\s]*$/;

export function validRole(role: unknown): boolean {
  return typeof role === "string" && ROLE.test(role);
}

function httpsUrl(v: unknown, field: string): string {
  if (typeof v !== "string" || !HTTPS_URL.test(v)) throw new ValidationError(`field '${field}' must be an https URL`);
  return v;
}

export function buildService(
  address: string,
  kind: string,
  roles: string[],
  opts: {
    title?: string;
    description?: string;
    docs?: string;
    extension?: Obj;
    default?: string;
    iat?: string | number;
    exp?: string | number;
  } = {},
): Obj {
  return pl("service", opts.iat, opts.exp, {
    address,
    kind,
    title: opts.title ?? address.split("@")[0],
    description: opts.description,
    docs: opts.docs,
    roles: [...roles],
    extension: opts.extension,
    default: opts.default ?? "on",
  });
}

export function validateService(p: unknown, hubDomain?: string): Obj {
  const o = base(p, "service");
  const [, domain] = splitAddress(needStr(o, "address"));
  if (hubDomain !== undefined && domain !== hubDomain.toLowerCase()) throw new ValidationError("service address is not on this hub");
  const kind = needStr(o, "kind");
  if (kind !== "custom" && !(SERVICE_WELL_KNOWN as readonly string[]).includes(kind)) {
    throw new ValidationError(`kind ${JSON.stringify(kind)} is neither well-known nor 'custom'`);
  }
  needStr(o, "title");
  if ("description" in o && typeof o.description !== "string") throw new ValidationError("field 'description' must be a string");
  if ("docs" in o) httpsUrl(o.docs, "docs");
  const roles = needList(o, "roles");
  if (!roles.every(validRole) || new Set(roles).size !== roles.length) throw new ValidationError("roles must be a list of distinct short names");
  if ("extension" in o) {
    const ext = needObj(o, "extension");
    httpsUrl(ext.git, "extension.git");
    if (typeof ext.commit !== "string" || !HEX_COMMIT.test(ext.commit)) {
      throw new ValidationError("extension.commit must be 40 or 64 lowercase hex digits");
    }
    const path = ext.path;
    if (
      typeof path !== "string" ||
      !path ||
      path.startsWith("/") ||
      path.includes("\\") ||
      path.includes("\x00") ||
      path.split("/").some((seg) => seg === "" || seg === "..")
    ) {
      throw new ValidationError("extension.path must be a relative path without '..' segments");
    }
  }
  if (!(SERVICE_DEFAULTS as readonly string[]).includes(needStr(o, "default"))) throw new ValidationError("default must be 'on' or 'off'");
  return o;
}

/** A service document signed (only) by the hub's signing key. Its directory entry is the caller's to check. */
export async function verifyService(doc: unknown, hubKey: Key, opts: { hubDomain?: string; now?: number } = {}): Promise<Obj> {
  if (isObject(doc) && (Array.isArray(doc.sigs) ? doc.sigs.length : 0) !== 1) {
    throw new SignatureError("a service document carries exactly one signature");
  }
  return validateService(
    await verifyDocument(doc, [hubKey], { expectedType: "service", required: [hubKey.kid], now: opts.now }),
    opts.hubDomain,
  );
}

/** The verified service payloads in a (verified) capabilities payload, and a reason for each entry skipped.
 * Each `extras` entry of kind `service` is checked against the hub's signing key (`hubKey`, else the one in the
 * payload) and the hub's domain; bad and duplicate entries are skipped, never thrown. */
export async function servicesIn(capabilities: Obj, opts: { hubKey?: Key | null; now?: number } = {}): Promise<[Obj[], string[]]> {
  const out: Obj[] = [];
  const skipped: string[] = [];
  const domain = capabilities.domain;
  const candidates = opts.hubKey ? [opts.hubKey] : (await keysOf(capabilities)).filter((k) => k.use === "sign" && k.alg === "Ed25519");
  const seen = new Set<string>();
  const extras: unknown[] = Array.isArray(capabilities.extras) ? capabilities.extras : [];
  for (let i = 0; i < extras.length; i++) {
    const e = extras[i];
    if (!isObject(e) || e.kind !== "service") continue;
    try {
      const doc = e.doc;
      if (!isObject(doc)) throw new ValidationError("no doc");
      let p: Obj | undefined;
      let last: unknown = null;
      for (const k of candidates) {
        try {
          p = await verifyService(doc, k, { hubDomain: typeof domain === "string" ? domain : undefined, now: opts.now });
          break;
        } catch (ex) {
          if (!(ex instanceof SignatureError)) throw ex;
          last = ex;
        }
      }
      if (p === undefined) throw last ?? new SignatureError("no hub key");
      const addr = p.address.toLowerCase();
      if (seen.has(addr)) throw new ValidationError("listed twice");
      seen.add(addr);
      out.push(p);
    } catch (ex) {
      // a bad entry never spoils the rest
      skipped.push(`extras[${i}]: ${ex instanceof ProtocolError ? ex.detail : String(ex)}`);
    }
  }
  return [out, skipped];
}

export function buildRoleGrant(service: string, holder: string, holderKid: string, role: string, iat?: string | number, exp?: string | number): Obj {
  return pl("role-grant", iat, exp, { service, holder, holder_kid: holderKid, role });
}

export function validateRoleGrant(p: unknown): Obj {
  const o = base(p, "role-grant");
  splitAddress(needStr(o, "service"));
  splitAddress(needStr(o, "holder"));
  kidField(o, "holder_kid");
  if (!validRole(o.role)) throw new ValidationError("role must be a short name");
  return o;
}

/** Signed by the service's key; `service`, `holder` and `holderKid` (the envelope's sender, as rotation
 * resolved it) are compared when given. `exp` is checked against `now`. */
export async function verifyRoleGrant(
  doc: unknown,
  serviceKey: Key,
  opts: { service?: string; holder?: string; holderKid?: string; now?: number } = {},
): Promise<Obj> {
  if (isObject(doc) && (Array.isArray(doc.sigs) ? doc.sigs.length : 0) !== 1) {
    throw new SignatureError("a role grant carries exactly one signature");
  }
  const p = validateRoleGrant(
    await verifyDocument(doc, [serviceKey], { expectedType: "role-grant", required: [serviceKey.kid], now: opts.now }),
  );
  for (const [name, want] of [
    ["service", opts.service],
    ["holder", opts.holder],
  ] as const) {
    if (want !== undefined && p[name].toLowerCase() !== want.toLowerCase()) throw new SignatureError(`role grant is for another ${name}`);
  }
  if (opts.holderKid !== undefined && p.holder_kid !== opts.holderKid) throw new SignatureError("role grant is for another key");
  return p;
}

/** A role grant as a signed document: shape and payload shape only (no signature check). */
export function validateRoleGrantDocument(doc: unknown): Obj {
  return validateRoleGrant(innerOf(doc, "role-grant", "role-grant"));
}

function validateServiceBody(kind: string, b: Obj): void {
  if (kind === "service-hello") {
    validateTokenDocument(b.token);
    needStr(b, "release");
  } else if (kind === "service-welcome") {
    validateTokenDocument(b.token);
    for (const g of needList(b, "roles")) validateRoleGrantDocument(g);
  } else if (kind === "service-call" || kind === "service-reply") {
    if (jsonBytes(b) > MAX_SERVICE_BODY) throw new ValidationError(`${kind} body is over 1 MiB`, "too_large");
    idField(b, "call");
    if (kind === "service-call") {
      if (!needStr(b, "method")) throw new ValidationError("method must not be empty");
      needObj(b, "args");
      if (b.role !== undefined && b.role !== null) validateRoleGrantDocument(b.role);
    } else {
      if (needBool(b, "ok")) {
        needObj(b, "result");
      } else {
        const err = needObj(b, "error");
        if (typeof err.code !== "string" || !err.code) throw new ValidationError("error.code missing");
        if ("detail" in err && typeof err.detail !== "string") throw new ValidationError("error.detail must be a string");
      }
    }
  } else if (kind === "service-event") {
    const event = needStr(b, "event");
    const data = needObj(b, "data");
    if (event === "roles") {
      for (const g of needList(data, "grants")) validateRoleGrantDocument(g);
    }
  }
}

// --- other bodies ------------------------------------------------------------------------------

export function buildInviteResponse(expires: string, hubName: string, nameHint: string | null = null, nameBound = false): Obj {
  return { v: VERSION, valid: true, expires, name_hint: nameHint, name_bound: nameBound, hub: hubName };
}

export function buildJoinResponse(address: string, hostnames: Obj, hubKey: Key, tunnel: string | null): Obj {
  return { v: VERSION, address, hostnames, hub_key: hubKey.toJSON(), tunnel };
}
