// D1 queries. Every multi-step change that must be atomic is a conditional UPDATE or a batch.
import type { Doc, Obj } from "./proto/docs";
import type { KeyJson } from "./proto/keys";
import { lookalikeKey } from "./proto/names";

export type Use = "sign" | "seal" | "recover";
export type KeySet = Record<Use, KeyJson>;
export interface Hostnames {
  instance: string;
  files: string;
}

export interface Member {
  name: string;
  address: string;
  status: "active" | "left";
  mode: "tunnel" | "own-url";
  keys: KeySet;
  kidHistory: string[];
  seq: number;
  selfDoc: Doc | null;
  hostnames: Hostnames;
  ownUrls: Hostnames | null;
  joined: number;
  release: string;
  leftAt: number | null;
  movedTo: string | null;
  movedDoc: Doc | null;
}

export interface Invite {
  code: string;
  expires: number;
  usedBy: string | null;
  nameHint: string | null;
  reregister: string | null;
}

type Row = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const json = <T>(s: unknown): T | null => (typeof s === "string" ? (JSON.parse(s) as T) : null);

function toMember(r: Row): Member {
  return {
    name: r.name,
    address: r.address,
    status: r.status,
    mode: r.mode,
    keys: JSON.parse(r.keys),
    kidHistory: JSON.parse(r.kid_history),
    seq: r.seq,
    selfDoc: json<Doc>(r.self_doc),
    hostnames: JSON.parse(r.hostnames),
    ownUrls: json<Hostnames>(r.own_urls),
    joined: r.joined,
    release: r.release,
    leftAt: r.left_at ?? null,
    movedTo: r.moved_to ?? null,
    movedDoc: json<Doc>(r.moved_doc),
  };
}

export async function getMember(db: D1Database, name: string): Promise<Member | null> {
  const r = await db.prepare("SELECT * FROM members WHERE name = ?1").bind(name).first<Row>();
  return r ? toMember(r) : null;
}

export async function nameExists(db: D1Database, name: string): Promise<boolean> {
  return (await db.prepare("SELECT 1 AS x FROM members WHERE name = ?1").bind(name).first()) !== null;
}

export async function lookalikeExists(db: D1Database, key: string): Promise<boolean> {
  return (await db.prepare("SELECT 1 AS x FROM members WHERE lookalike = ?1").bind(key).first()) !== null;
}

// --- invites ---------------------------------------------------------------------------------

export async function getInvite(db: D1Database, code: string): Promise<Invite | null> {
  const r = await db.prepare("SELECT * FROM invites WHERE code = ?1").bind(code).first<Row>();
  return r ? { code: r.code, expires: r.expires, usedBy: r.used_by ?? null, nameHint: r.name_hint ?? null, reregister: r.reregister ?? null } : null;
}

export async function createInvite(
  db: D1Database,
  code: string,
  expires: number,
  now: number,
  opts: { nameHint?: string | null; reregister?: string | null },
): Promise<void> {
  await db
    .prepare("INSERT INTO invites (code, expires, used_by, name_hint, reregister, created) VALUES (?1, ?2, NULL, ?3, ?4, ?5)")
    .bind(code, expires, opts.nameHint ?? null, opts.reregister ?? null, now)
    .run();
}

/** Use an invite exactly once, even when two registrations race. True if this call won it. */
export async function claimInvite(db: D1Database, code: string, name: string, now: number): Promise<boolean> {
  const r = await db
    .prepare("UPDATE invites SET used_by = ?2 WHERE code = ?1 AND used_by IS NULL AND expires >= ?3")
    .bind(code, name, now)
    .run();
  return (r.meta.changes ?? 0) === 1;
}

/** A refused registration gives its invite back (the fake hub does the same). */
export async function releaseInvite(db: D1Database, code: string, name: string): Promise<void> {
  await db.prepare("UPDATE invites SET used_by = NULL WHERE code = ?1 AND used_by = ?2").bind(code, name).run();
}

// --- members ---------------------------------------------------------------------------------

export function isUniqueViolation(e: unknown): boolean {
  return /UNIQUE constraint failed|constraint failed/i.test(String((e as Error)?.message ?? e));
}

export interface NewMember {
  name: string;
  address: string;
  mode: "tunnel" | "own-url";
  keys: KeySet;
  hostnames: Hostnames;
  ownUrls: Hostnames | null;
  release: string;
  now: number;
}

/** Insert a new member; throws a unique violation if the name (or a lookalike) exists. */
export async function insertMember(db: D1Database, m: NewMember): Promise<void> {
  await db
    .prepare(
      "INSERT INTO members (name, lookalike, address, status, mode, keys, kid_history, seq, self_doc, hostnames, own_urls, joined, release) " +
        "VALUES (?1, ?2, ?3, 'active', ?4, ?5, ?6, -1, NULL, ?7, ?8, ?9, ?10)",
    )
    .bind(
      m.name,
      lookalikeKey(m.name),
      m.address,
      m.mode,
      JSON.stringify(m.keys),
      JSON.stringify(Object.values(m.keys).map((k) => k.kid)),
      JSON.stringify(m.hostnames),
      m.ownUrls ? JSON.stringify(m.ownUrls) : null,
      m.now,
      m.release,
    )
    .run();
}

/** Re-registration (both keys lost): the member keeps its name, gets new keys and no self document
 * until it publishes one; the hub's reregistration and revocation documents are added in the same batch. */
export async function reregisterMember(
  db: D1Database,
  m: NewMember,
  oldKidHistory: string[],
  reregistrationDoc: Doc,
  revocationDocs: Doc[],
): Promise<void> {
  const stmts = [
    db
      .prepare(
        "UPDATE members SET mode = ?2, keys = ?3, kid_history = ?4, seq = -1, self_doc = NULL, hostnames = ?5, own_urls = ?6, " +
          "joined = ?7, release = ?8 WHERE name = ?1 AND status = 'active'",
      )
      .bind(
        m.name,
        m.mode,
        JSON.stringify(m.keys),
        JSON.stringify([...oldKidHistory, ...Object.values(m.keys).map((k) => k.kid)]),
        JSON.stringify(m.hostnames),
        m.ownUrls ? JSON.stringify(m.ownUrls) : null,
        m.now,
        m.release,
      ),
    db.prepare("INSERT INTO reregistrations (name, doc) VALUES (?1, ?2)").bind(m.name, JSON.stringify(reregistrationDoc)),
    ...revocationDocs.map((d) => db.prepare("INSERT INTO revocations (name, doc) VALUES (?1, ?2)").bind(m.name, JSON.stringify(d))),
  ];
  await db.batch(stmts);
}

/** Store an accepted self document if the row is still the one the caller verified against
 * (same `seq`). Returns false if it changed meanwhile. */
export async function updateSelf(
  db: D1Database,
  name: string,
  expectedSeq: number,
  seq: number,
  doc: Doc,
  keys: KeySet,
  kidHistory: string[],
): Promise<boolean> {
  const r = await db
    .prepare(
      "UPDATE members SET seq = ?3, self_doc = ?4, keys = ?5, kid_history = ?6 WHERE name = ?1 AND status = 'active' AND seq = ?2",
    )
    .bind(name, expectedSeq, seq, JSON.stringify(doc), JSON.stringify(keys), JSON.stringify(kidHistory))
    .run();
  return (r.meta.changes ?? 0) === 1;
}

export async function markLeft(db: D1Database, name: string, now: number, movedTo: string | null, movedDoc: Obj | null): Promise<boolean> {
  const r = await db
    .prepare("UPDATE members SET status = 'left', left_at = ?2, moved_to = ?3, moved_doc = ?4 WHERE name = ?1 AND status = 'active'")
    .bind(name, now, movedTo, movedDoc ? JSON.stringify(movedDoc) : null)
    .run();
  return (r.meta.changes ?? 0) === 1;
}

export async function listDocs(db: D1Database, kind: "revocations" | "reregistrations", name: string): Promise<Doc[]> {
  const q = kind === "revocations" ? "SELECT doc FROM revocations WHERE name = ?1 ORDER BY id" : "SELECT doc FROM reregistrations WHERE name = ?1 ORDER BY id";
  const r = await db.prepare(q).bind(name).all<Row>();
  return r.results.map((x) => JSON.parse(x.doc));
}
