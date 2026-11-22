// Payload builders, validators and typed verifiers for every document type the hub handles.
// Validators check required fields and types and ignore unknown fields (versions.md). They throw
// ValidationError. The verify* functions combine signature, outer checks and validation.
import * as b64 from "./b64";
import { VERSION } from "./consts";
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
import { DocumentError, SignatureError, ValidationError } from "./errors";
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

// --- moved -------------------------------------------------------------------------------------

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

// --- other bodies ------------------------------------------------------------------------------

export function buildInviteResponse(expires: string, hubName: string, nameHint: string | null = null, nameBound = false): Obj {
  return { v: VERSION, valid: true, expires, name_hint: nameHint, name_bound: nameBound, hub: hubName };
}

export function buildJoinResponse(address: string, hostnames: Obj, hubKey: Key, tunnel: string | null): Obj {
  return { v: VERSION, address, hostnames, hub_key: hubKey.toJSON(), tunnel };
}
