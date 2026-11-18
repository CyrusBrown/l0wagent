// The hub's core HTTP API (protocol/openapi.json) plus the operator's invite endpoint.
import { acceleratorExtra, disableForMember, handleAccelerator } from "./accelerator";
import { type Config, hostnameLayout, hostnamesFor, hubKey, releaseLess } from "./config";
import * as db from "./db";
import type { Env } from "./env";
import * as b64 from "./proto/b64";
import { SUPPORTED_VERSIONS, VERSION } from "./proto/consts";
import * as docs from "./proto/docs";
import type { Doc, Obj } from "./proto/docs";
import { UnsupportedVersion, NameRuleError } from "./proto/errors";
import { formatInvite } from "./proto/invite";
import { Key } from "./proto/keys";
import { checkName, validateName } from "./proto/names";
import * as P from "./proto/payloads";
import { checkFresh, formatTime, now } from "./proto/times";
import { errorResponse, jsonResponse } from "./respond";

const USES = ["sign", "seal", "recover"] as const;
type Use = (typeof USES)[number];

export class HubError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail = "",
    readonly extra: Record<string, unknown> = {},
    readonly headers: Record<string, string> = {},
  ) {
    super(detail);
  }
}

/** Run a protocol function; a failure becomes `400 <code>` (or `unsupported_version`). */
async function parseSigned<T>(fn: () => T | Promise<T>, code: string): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof HubError) throw e;
    if (e instanceof UnsupportedVersion) throw new HubError(400, "unsupported_version", e.message, { supported: [...SUPPORTED_VERSIONS] });
    throw new HubError(400, code, (e as Error).message || (e as Error).name);
  }
}

const MAX_BODY = 512 * 1024; // every JSON body this API takes is far smaller

async function readText(request: Request): Promise<string> {
  const declared = Number.parseInt(request.headers.get("content-length") ?? "0", 10);
  if (declared > MAX_BODY) {
    await request.body?.cancel().catch(() => undefined); // so the runtime answers at once
    throw new HubError(413, "payload_too_large", "request body is too large");
  }
  const reader = request.body?.getReader();
  if (!reader) return "";
  const parts: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const r = await reader.read();
    if (r.done) break;
    total += r.value.length;
    if (total > MAX_BODY) {
      await reader.cancel();
      throw new HubError(413, "payload_too_large", "request body is too large");
    }
    parts.push(r.value);
  }
  return new TextDecoder().decode(b64.concat(...parts));
}

async function readBody(request: Request, code: string): Promise<Obj> {
  let doc: unknown;
  try {
    doc = JSON.parse(await readText(request));
  } catch (e) {
    if (e instanceof HubError) throw e;
    throw new HubError(400, code, "body is not JSON");
  }
  if (!docs.isObject(doc)) throw new HubError(400, code, "body must be a JSON object");
  return doc;
}

function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => k in (b as object) && sameJson((a as Obj)[k], (b as Obj)[k]));
}

const address = (cfg: Config, name: string): string => `${name}@${cfg.domain}`;

function memberOrRaise(m: db.Member | null): db.Member {
  if (!m) throw new HubError(404, "no_such_member", "no such member");
  if (m.status === "left") throw new HubError(410, "left", "this member has left");
  return m;
}

async function dropTunnel(env: Env, name: string, code: number, permanent: boolean): Promise<void> {
  await env.MEMBER.get(env.MEMBER.idFromName(name)).drop(code, permanent);
}

// --- capabilities ------------------------------------------------------------------------------

async function capabilities(env: Env, cfg: Config): Promise<Response> {
  const key = await hubKey(env);
  const payload = P.buildHub({
    name: cfg.name,
    domain: cfg.domain,
    keys: [key.key],
    api: cfg.api,
    tunnel: cfg.tunnelUrl,
    minRelease: cfg.minRelease,
    membership: { invites: true, signups: "closed", own_url: cfg.ownUrl, require_2fa: true },
    hostnames: { layout: hostnameLayout(cfg), example: hostnamesFor(cfg, "mara") },
    policies: cfg.policies,
    limits: cfg.limits,
    extras: acceleratorExtra(cfg) ? [acceleratorExtra(cfg) as Obj] : [],
  });
  const doc = await docs.signDocument("hub", payload, [key]);
  return jsonResponse(doc, 200, { "cache-control": "max-age=300" });
}

// --- invites -----------------------------------------------------------------------------------

function inviteState(inv: db.Invite | null): db.Invite {
  if (!inv) throw new HubError(404, "no_such_invite", "no such invite");
  if (inv.usedBy) throw new HubError(410, "invite_used", "this invite was already used");
  if (inv.expires < now()) throw new HubError(410, "invite_expired", "this invite has expired");
  return inv;
}

async function getInvite(env: Env, cfg: Config, code: string): Promise<Response> {
  const inv = inviteState(await db.getInvite(env.DB, code));
  return jsonResponse(P.buildInviteResponse(formatTime(inv.expires), cfg.name, inv.nameHint, inv.bind !== null));
}

// --- joining -----------------------------------------------------------------------------------

/** Invite and name rules, run before and again (atomically, by claiming the invite) at commit. */
async function admissionChecks(env: Env, cfg: Config, p: Obj): Promise<void> {
  const name: string = p.name;
  const inv = await db.getInvite(env.DB, p.invite);
  if (!inv) throw new HubError(403, "needs_invite", "a valid invite is required");
  inviteState(inv);
  if (p.reregister === true) {
    const m = await db.getMember(env.DB, name);
    if (inv.reregister !== name || !m || m.status !== "active") {
      throw new HubError(403, "needs_invite", "this invite does not approve re-registering that name");
    }
    return;
  }
  if (inv.bind !== null && inv.bind !== name) throw new HubError(409, "name_bound", "this invite is bound to another name");
  try {
    await checkName(name, {
      bound: inv.bind !== null,
      reservedExtra: cfg.reservedExtra,
      isTaken: (n) => db.nameExists(env.DB, n),
      lookalikeTaken: (k) => db.lookalikeExists(env.DB, k),
    });
  } catch (e) {
    if (e instanceof NameRuleError) throw new HubError(409, e.code, e.detail);
    throw e;
  }
}

export async function checkOwnUrls(cfg: Config, p: Obj, signKey: Key): Promise<void> {
  if (!cfg.ownUrl) throw new HubError(400, "bad_registration", "this hub does not accept members with their own URLs");
  const urls = p.own_urls as { instance: string; files: string };
  let a: URL;
  let b: URL;
  try {
    a = new URL(urls.instance);
    b = new URL(urls.files);
  } catch {
    throw new HubError(422, "own_url_unreachable", "an own URL is not a valid URL");
  }
  if (a.origin === b.origin) throw new HubError(422, "own_url_unreachable", "the instance and files URLs must be different origins");
  try {
    const r = await fetch(urls.instance.replace(/\/+$/, "") + "/.well-known/l0wagent/self", {
      redirect: "manual",
      signal: AbortSignal.timeout(5000),
    });
    if (r.status !== 200) throw new HubError(422, "own_url_unreachable", `self document answered ${r.status}`);
    const sp = await P.verifySelf(await r.json());
    if (sp.address !== address(cfg, p.name) || (await P.selfSigningKey(sp)).kid !== signKey.kid) {
      throw new HubError(422, "own_url_unreachable", "the served self document is not this member's");
    }
    const f = await fetch(urls.files, { redirect: "manual", signal: AbortSignal.timeout(5000) }); // any answer over HTTPS counts
    await f.body?.cancel();
  } catch (e) {
    if (e instanceof HubError) throw e;
    throw new HubError(422, "own_url_unreachable", `${(e as Error).name}: ${(e as Error).message}`);
  }
}

async function register(request: Request, env: Env, cfg: Config): Promise<Response> {
  const doc = await readBody(request, "bad_registration");
  const p = await parseSigned(() => P.verifyRegistration(doc, cfg.domain), "bad_registration");
  const parsed = await Promise.all((p.keys as unknown[]).map((k) => Key.fromJSON(k)));
  const keySet = {} as Record<Use, Key>;
  for (const u of USES) keySet[u] = parsed.find((k) => k.use === u && k.usable) as Key;
  if (releaseLess(p.instance.release, cfg.minRelease)) {
    throw new HubError(426, "instance_too_old", `this hub needs instance release ${cfg.minRelease} or newer`);
  }
  await admissionChecks(env, cfg, p);
  const name: string = p.name;
  let hostnames: db.Hostnames;
  if (p.mode === "own-url") {
    await checkOwnUrls(cfg, p, keySet.sign);
    hostnames = {
      instance: new URL(p.own_urls.instance).host.toLowerCase(),
      files: new URL(p.own_urls.files).host.toLowerCase(),
    };
  } else {
    hostnames = hostnamesFor(cfg, name);
  }
  const t = now();
  const keys = Object.fromEntries(USES.map((u) => [u, keySet[u].toJSON()])) as db.KeySet;
  const newMember: db.NewMember = {
    name,
    address: address(cfg, name),
    mode: p.mode,
    keys,
    hostnames,
    ownUrls: p.mode === "own-url" ? p.own_urls : null,
    release: p.instance.release,
    now: t,
  };

  // Commit: the invite is claimed first (exactly one registration wins it), then the member is written.
  if (!(await db.claimInvite(env.DB, p.invite, name, t))) {
    await admissionChecks(env, cfg, p); // says why (used, expired, unknown)
    throw new HubError(410, "invite_used", "this invite was already used");
  }
  const reregistered = p.reregister === true;
  try {
    if (reregistered) {
      const old = memberOrRaise(await db.getMember(env.DB, name));
      const oldKids = Object.values(old.keys).map((k) => k.kid);
      const hub = await hubKey(env);
      const rr = await docs.signDocument("reregistration", P.buildReregistration(newMember.address, oldKids, Object.values(keys).map((k) => k.kid)), [hub]);
      const revs: Doc[] = [];
      for (const kid of oldKids) revs.push(await docs.signDocument("revocation", P.buildRevocation(newMember.address, kid, "re-registered"), [hub]));
      await db.reregisterMember(env.DB, newMember, old.kidHistory, rr, revs);
    } else {
      await db.insertMember(env.DB, newMember);
    }
  } catch (e) {
    await db.releaseInvite(env.DB, p.invite, name);
    if (db.isUniqueViolation(e)) throw new HubError(409, "name_taken", "name is taken");
    throw e;
  }
  if (reregistered) {
    await disableForMember(env, cfg, name);
    await dropTunnel(env, name, 4401, false);
  }
  const tunnel = p.mode === "tunnel" ? cfg.tunnelUrl : null;
  return jsonResponse(P.buildJoinResponse(newMember.address, hostnames, (await hubKey(env)).key, tunnel), 201);
}

// --- self description --------------------------------------------------------------------------

/** Apply the rotations in `history` to the hub's record; return the keys they lead to. */
async function walkHistory(m: db.Member, payload: Obj): Promise<Record<Use, Key>> {
  const cur = {} as Record<Use, Key>;
  for (const u of USES) cur[u] = await Key.fromJSON(m.keys[u]);
  const seen = new Set(m.kidHistory);
  for (const rdoc of payload.history as Doc[]) {
    const rp = docs.parsePayload(docs.shape(rdoc));
    if (rp.type !== "rotation" || rp.address !== m.address) throw new HubError(400, "bad_self", "history holds a rotation for another address");
    const nw = await Key.fromJSON(rp.new);
    const use = USES.find((u) => cur[u].kid === rp.old);
    if (use === undefined) {
      if (seen.has(nw.kid)) continue; // applied by an earlier self
      throw new HubError(400, "bad_self", "rotation chain does not start at a key the hub knows");
    }
    if (nw.use !== use) throw new HubError(400, "bad_self", "rotation changes the use of a key");
    await parseSigned(() => P.verifyRotation(rdoc, cur[use], use === "recover" ? null : cur.recover), "bad_self");
    cur[use] = nw;
    seen.add(nw.kid);
  }
  return cur;
}

async function putSelf(request: Request, env: Env, cfg: Config, name: string): Promise<Response> {
  const m = memberOrRaise(await db.getMember(env.DB, name));
  const doc = await readBody(request, "bad_self");
  const p = await parseSigned(() => P.verifySelf(doc), "bad_self");
  if (p.address !== m.address) throw new HubError(400, "bad_self", "address does not match the member");
  if (!sameJson(p.hostnames, m.hostnames)) throw new HubError(400, "bad_self", "hostnames do not match what the hub assigned");
  if (m.mode === "own-url" && !sameJson(p.own_urls ?? null, m.ownUrls)) {
    throw new HubError(400, "bad_self", "own_urls do not match the registration");
  }
  const expected = await parseSigned(() => walkHistory(m, p), "bad_self");
  const given = {} as Record<Use, Key>;
  for (const k of await Promise.all((p.keys as unknown[]).map((x) => Key.fromJSON(x)))) {
    if (k.usable && (USES as readonly string[]).includes(k.use)) given[k.use as Use] = k;
  }
  if (USES.some((u) => given[u].kid !== expected[u].kid)) {
    throw new HubError(400, "bad_self", "keys differ from the hub's record and no valid rotation explains it");
  }
  if (p.seq <= m.seq) throw new HubError(409, "stale_seq", `seq must be higher than ${m.seq}`);
  const keys = Object.fromEntries(USES.map((u) => [u, given[u].toJSON()])) as db.KeySet;
  const kids = [...new Set([...m.kidHistory, ...USES.map((u) => given[u].kid)])].sort();
  if (!(await db.updateSelf(env.DB, name, m.seq, p.seq, doc as Doc, keys, kids))) {
    memberOrRaise(await db.getMember(env.DB, name));
    throw new HubError(409, "stale_seq", "the record changed while this was processed; fetch it and try again");
  }
  return jsonResponse(await docs.countersignDirectory(doc as Doc, await hubKey(env)));
}

// --- directory ---------------------------------------------------------------------------------

async function getEntry(env: Env, name: string): Promise<Response> {
  const m = await db.getMember(env.DB, name);
  if (!m || (m.status === "active" && m.selfDoc === null)) {
    throw new HubError(404, "no_such_member", "no such member (or it has not published yet)");
  }
  if (m.status === "left") {
    if (m.movedDoc) return jsonResponse(m.movedDoc, 301, { "cache-control": "max-age=3600" });
    throw new HubError(410, "left", "this member has left");
  }
  return jsonResponse(await docs.countersignDirectory(m.selfDoc as Doc, await hubKey(env)), 200, { "cache-control": "no-cache" });
}

async function getList(env: Env, name: string, kind: "revocations" | "reregistrations"): Promise<Response> {
  if (!(await db.nameExists(env.DB, name))) throw new HubError(404, "no_such_member", "no such member");
  return jsonResponse(await db.listDocs(env.DB, kind, name));
}

// --- leaving -----------------------------------------------------------------------------------

async function leave(request: Request, env: Env, cfg: Config, name: string): Promise<Response> {
  const m = memberOrRaise(await db.getMember(env.DB, name));
  const signKey = await Key.fromJSON(m.keys.sign);
  const body = await readBody(request, "bad_leave");
  let movedDoc: Obj | null = (body.moved as Obj | undefined) ?? null;
  delete body.moved;
  const p = await parseSigned(() => P.verifyLeave(body, signKey), "bad_leave");
  await parseSigned(() => checkFresh(p.iat), "bad_leave");
  if (p.address !== address(cfg, name)) throw new HubError(400, "bad_leave", "address does not match the member");
  if (movedDoc !== null && p.moved_to !== null) {
    const mp = await parseSigned(() => P.verifyMoved(movedDoc, signKey), "bad_leave");
    if (mp.from !== p.address || mp.to !== p.moved_to) throw new HubError(400, "bad_leave", "moved document does not match the leave document");
  } else {
    movedDoc = null;
  }
  if (!(await db.markLeft(env.DB, name, now(), p.moved_to, movedDoc))) memberOrRaise(await db.getMember(env.DB, name));
  await disableForMember(env, cfg, name);
  await dropTunnel(env, name, 1000, true);
  return jsonResponse({ v: VERSION, left: true });
}

// --- the operator's invite endpoint (not part of the spec) -------------------------------------

async function sameSecret(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([a, b].map(async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", b64.utf8(s)))));
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

async function adminInvite(request: Request, env: Env, cfg: Config): Promise<Response> {
  const token = env.ADMIN_TOKEN?.trim();
  if (!token) throw new HubError(404, "not_found", "no such endpoint");
  const given = /^Bearer (.+)$/.exec(request.headers.get("authorization") ?? "")?.[1] ?? "";
  if (!(await sameSecret(given, token))) throw new HubError(401, "unauthorized", "a valid admin token is required", {}, { "www-authenticate": "Bearer" });
  let o: Obj = {};
  const text = await readText(request);
  if (text.trim()) {
    try {
      o = JSON.parse(text);
    } catch {
      throw new HubError(400, "bad_request", "body is not JSON");
    }
    if (!docs.isObject(o)) throw new HubError(400, "bad_request", "body must be a JSON object");
  }
  const days = o.days ?? cfg.inviteDays;
  if (!Number.isInteger(days) || days < 1 || days > 365) throw new HubError(400, "bad_request", "days must be an integer from 1 to 365");
  let nameHint: string | null = null;
  if (o.name_hint !== undefined && o.name_hint !== null) {
    try {
      nameHint = validateName(o.name_hint);
    } catch {
      throw new HubError(400, "bad_request", "name_hint is not a valid name");
    }
  }
  let rereg: string | null = null;
  if (o.reregister !== undefined && o.reregister !== null) {
    const m = typeof o.reregister === "string" ? await db.getMember(env.DB, o.reregister) : null;
    if (!m || m.status !== "active") throw new HubError(400, "bad_request", "reregister must name an active member");
    rereg = m.name;
  }
  let bind: string | null = null;
  if (o.bind !== undefined && o.bind !== null) {
    if (rereg) throw new HubError(400, "bad_request", "bind and reregister cannot be combined");
    if (nameHint !== null && nameHint !== o.bind) throw new HubError(400, "bad_request", "name_hint and bind must be equal");
    try {
      bind = validateName(o.bind);
    } catch {
      throw new HubError(400, "bad_request", "bind is not a valid name");
    }
    try {
      // the hub's own and extra reserved names may be bound; the spec's list and taken names (and lookalikes) may not
      await checkName(bind, {
        bound: true,
        reservedExtra: cfg.reservedExtra,
        isTaken: (n) => db.nameExists(env.DB, n),
        lookalikeTaken: (k) => db.lookalikeExists(env.DB, k),
      });
    } catch (e) {
      if (e instanceof NameRuleError) throw new HubError(400, "bad_request", `cannot bind that name: ${e.detail}`);
      throw e;
    }
    nameHint = bind;
  }
  const code = b64.encode(crypto.getRandomValues(new Uint8Array(24))); // 192 bits
  const t = now();
  await db.createInvite(env.DB, code, t + days * 86400, t, { nameHint, reregister: rereg, bind });
  return jsonResponse({ invite: formatInvite(cfg.baseUrl, code), expires: formatTime(t + days * 86400) }, 201);
}

// --- routing -----------------------------------------------------------------------------------

const WELL_KNOWN_MEMBER = /^\/\.well-known\/l0wagent\/members\/([^/]+)(?:\/(revocations|reregistrations))?$/;
const MEMBER_PATH = /^\/v0\/members\/([^/]+)\/(self|leave)$/;
const INVITE_PATH = /^\/v0\/invites\/([^/]+)$/;

function allow(request: Request, ...methods: string[]): void {
  const m = request.method === "HEAD" ? "GET" : request.method;
  if (!methods.includes(m)) throw new HubError(405, "method_not_allowed", `use ${methods.join(" or ")}`, {}, { allow: methods.join(", ") });
}

function decodeSeg(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export async function handleApi(request: Request, env: Env, cfg: Config): Promise<Response> {
  const path = new URL(request.url).pathname;
  try {
    if (path === "/.well-known/l0wagent/hub") {
      allow(request, "GET");
      return await capabilities(env, cfg);
    }
    let m = WELL_KNOWN_MEMBER.exec(path);
    if (m) {
      allow(request, "GET");
      const name = decodeSeg(m[1]);
      return m[2] ? await getList(env, name, m[2] as "revocations" | "reregistrations") : await getEntry(env, name);
    }
    m = INVITE_PATH.exec(path);
    if (m) {
      allow(request, "GET");
      return await getInvite(env, cfg, decodeSeg(m[1]));
    }
    if (path === "/v0/members") {
      allow(request, "POST");
      return await register(request, env, cfg);
    }
    m = MEMBER_PATH.exec(path);
    if (m) {
      const name = decodeSeg(m[1]);
      if (m[2] === "self") {
        allow(request, "PUT");
        return await putSelf(request, env, cfg, name);
      }
      allow(request, "POST");
      return await leave(request, env, cfg, name);
    }
    if (path === "/v0/accelerators/cloudflared") {
      if (!cfg.accelerator) throw new HubError(404, "not_found", "no such endpoint");
      allow(request, "POST");
      return await handleAccelerator(request, env, cfg, await readBody(request, "bad_request"));
    }
    if (path === "/v0/admin/invites") {
      allow(request, "POST");
      return await adminInvite(request, env, cfg);
    }
    throw new HubError(404, "not_found", "no such endpoint");
  } catch (e) {
    if (e instanceof HubError) return errorResponse(e.status, e.code, e.detail, e.extra, e.headers);
    console.error("unexpected error:", e);
    return errorResponse(500, "internal_error", "something went wrong on the hub");
  }
}
