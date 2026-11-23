// Member names (membership.md).
import { NameRuleError } from "./errors";

const NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;
export const MIN_LEN = 3;
export const MAX_LEN = 32;

export const RESERVED: ReadonlySet<string> = new Set(
  (
    "admin api www hub files web mail email board spaces rooms relay status help support abuse " +
    "postmaster root security tunnel directory well-known"
  ).split(" "),
);

const LOOKALIKE: Record<string, string> = { "0": "o", "1": "l", i: "l", "5": "s", "-": "" };

/** Throw NameRuleError (`bad_registration`) unless `name` follows the syntax rules. */
export function validateName(name: unknown): string {
  if (typeof name !== "string" || name.length < MIN_LEN || name.length > MAX_LEN) {
    throw new NameRuleError(`name must be ${MIN_LEN} to ${MAX_LEN} characters`);
  }
  if (name.includes("--") || !NAME_RE.test(name)) {
    throw new NameRuleError("name must be lowercase letters and digits with single dashes between them");
  }
  return name;
}

/** Map 0->o, 1->l, i->l, 5->s and remove dashes. */
export function lookalikeKey(name: string): string {
  let out = "";
  for (const c of name) out += c in LOOKALIKE ? LOOKALIKE[c] : c;
  return out;
}

export interface NameChecks {
  /** The hub's own extra reserved names (services, its own name, RESERVED_NAMES). */
  reservedExtra?: Iterable<string>;
  /** Is this exact name a member's (including members that left: names are never reused)? */
  isTaken: (name: string) => Promise<boolean>;
  /** A bound invite for this name (membership.md): the hub's own and extra reserved names don't refuse it. */
  bound?: boolean;
  /** Does any member's name map to this lookalike key? */
  lookalikeTaken: (key: string) => Promise<boolean>;
}

/** Reserved by the spec's fixed list, exactly or as a lookalike: no invite can bind such a name. */
export function isSpecReserved(name: string): boolean {
  const key = lookalikeKey(name);
  for (const r of RESERVED) if (lookalikeKey(r) === key) return true;
  return false;
}

/** Full hub-side check. Codes: bad_registration (syntax), name_reserved, name_taken. */
export async function checkName(name: string, c: NameChecks): Promise<string> {
  validateName(name);
  const reserved = new Set([...RESERVED, ...(c.bound ? [] : (c.reservedExtra ?? []))]);
  if (reserved.has(name)) throw new NameRuleError("name is reserved", "name_reserved");
  if (await c.isTaken(name)) throw new NameRuleError("name is taken", "name_taken");
  const key = lookalikeKey(name);
  for (const r of reserved) {
    if (lookalikeKey(r) === key) throw new NameRuleError("name looks like a reserved name", "name_reserved");
  }
  if (await c.lookalikeTaken(key)) throw new NameRuleError("name looks like an existing name", "name_taken");
  return name;
}
