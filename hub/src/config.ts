// The hub's configuration, entirely from wrangler vars and secrets. Nothing here names a domain.
import type { Env } from "./env";
import * as b64 from "./proto/b64";
import type { Obj } from "./proto/docs";
import { type KeyPair, fromSeed } from "./proto/keys";

export const DEFAULT_LIMITS = { tunnel_streams: 64, request_body_bytes: 104857600, auth_rate_per_minute: 20 };

export class ConfigError extends Error {}

export interface Limits {
  tunnel_streams: number;
  request_body_bytes: number;
  auth_rate_per_minute: number;
  [extra: string]: unknown;
}

export interface Config {
  name: string;
  baseUrl: string; // scheme://host[:port], no trailing slash
  baseHost: string; // host[:port] of the base URL
  domain: string; // the hub domain in addresses
  api: string;
  tunnelUrl: string;
  tunnelHost: string;
  instancePattern: string;
  filesPattern: string;
  instanceRe: RegExp;
  filesRe: RegExp;
  reservedExtra: string[];
  minRelease: string;
  limits: Limits;
  policies?: Obj;
  ownUrl: boolean;
  inviteDays: number;
  keyCreated: string;
}

const cache = new WeakMap<object, Config>();

function parseJsonObject(v: string | undefined, what: string): Obj | undefined {
  if (v === undefined || v.trim() === "") return undefined;
  let o: unknown;
  try {
    o = JSON.parse(v);
  } catch {
    throw new ConfigError(`${what} is not valid JSON`);
  }
  if (o === null || typeof o !== "object" || Array.isArray(o)) throw new ConfigError(`${what} must be a JSON object`);
  return o as Obj;
}

function patternRe(pattern: string, what: string): RegExp {
  if (pattern.split("{name}").length !== 2) throw new ConfigError(`${what} must contain {name} exactly once`);
  const [pre, post] = pattern.toLowerCase().split("{name}");
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // a valid name: single dashes only, so `files--mara` is never taken for an instance name
  return new RegExp(`^${esc(pre)}([a-z0-9]+(?:-[a-z0-9]+)*)${esc(post)}$`);
}

export function loadConfig(env: Env): Config {
  const hit = cache.get(env);
  if (hit) return hit;
  const need = (k: keyof Env): string => {
    const v = env[k];
    if (typeof v !== "string" || v.trim() === "") throw new ConfigError(`${k} is not set`);
    return v.trim();
  };
  let base: URL;
  try {
    base = new URL(need("HUB_BASE_URL"));
  } catch {
    throw new ConfigError("HUB_BASE_URL is not a URL");
  }
  if ((base.protocol !== "https:" && base.protocol !== "http:") || (base.pathname !== "/" && base.pathname !== "") || base.search || base.hash) {
    throw new ConfigError("HUB_BASE_URL must be scheme://host[:port]");
  }
  const baseHost = base.host.toLowerCase();
  const domain = (env.HUB_DOMAIN?.trim() || baseHost).toLowerCase();
  const tunnelUrl = need("TUNNEL_URL");
  let tunnelHost: string;
  try {
    tunnelHost = new URL(tunnelUrl).host.toLowerCase();
  } catch {
    throw new ConfigError("TUNNEL_URL is not a URL");
  }
  const lim = parseJsonObject(env.LIMITS, "LIMITS") ?? {};
  const limits = { ...DEFAULT_LIMITS, ...lim } as Limits;
  for (const k of ["tunnel_streams", "request_body_bytes", "auth_rate_per_minute"] as const) {
    if (!Number.isInteger(limits[k]) || limits[k] < 0) throw new ConfigError(`LIMITS.${k} must be a non-negative integer`);
  }
  const instancePattern = need("INSTANCE_HOST_PATTERN").toLowerCase();
  const filesPattern = need("FILES_HOST_PATTERN").toLowerCase();
  const reserved = (env.RESERVED_NAMES ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  // the hub's own name (the first label of its domain) is reserved too (membership.md)
  reserved.push(domain.split(":")[0].split(".")[0]);
  const days = Number(env.INVITE_DAYS ?? 7);
  const cfg: Config = {
    name: need("HUB_NAME"),
    baseUrl: `${base.protocol}//${baseHost}`,
    baseHost,
    domain,
    api: `${base.protocol}//${baseHost}/v0/`,
    tunnelUrl,
    tunnelHost,
    instancePattern,
    filesPattern,
    instanceRe: patternRe(instancePattern, "INSTANCE_HOST_PATTERN"),
    filesRe: patternRe(filesPattern, "FILES_HOST_PATTERN"),
    reservedExtra: reserved,
    minRelease: env.MIN_RELEASE?.trim() || "0.8.0",
    limits,
    policies: parseJsonObject(env.POLICIES, "POLICIES"),
    ownUrl: (env.OWN_URL ?? "true").trim().toLowerCase() !== "false",
    inviteDays: Number.isFinite(days) && days > 0 ? Math.floor(days) : 7,
    keyCreated: env.HUB_KEY_CREATED?.trim() || "2026-11-17T00:00:00Z",
  };
  cache.set(env, cfg);
  return cfg;
}

export function hostnamesFor(cfg: Config, name: string): { instance: string; files: string } {
  return {
    instance: cfg.instancePattern.replace("{name}", name),
    files: cfg.filesPattern.replace("{name}", name),
  };
}

/** Which member's hostname is this (by the configured patterns)? A prefilter: the member's stored
 * hostnames decide. */
export function matchHost(cfg: Config, host: string): { name: string; kind: "instance" | "files" } | null {
  const h = host.toLowerCase();
  const a = cfg.instanceRe.exec(h);
  if (a) return { name: a[1], kind: "instance" };
  const f = cfg.filesRe.exec(h);
  if (f) return { name: f[1], kind: "files" };
  return null;
}

/** Informative only (capabilities.md): "two-domains", "flat" or "nested". */
export function hostnameLayout(cfg: Config): string {
  const ex = hostnamesFor(cfg, "mara");
  const site = (h: string) => h.split(":")[0].split(".").slice(1).join(".");
  if (ex.files.endsWith("." + ex.instance)) return "nested";
  return site(ex.instance) === site(ex.files) ? "flat" : "two-domains";
}

const keyCache = new WeakMap<object, Promise<KeyPair>>();

/** The hub's signing key, derived from the HUB_SIGNING_SEED secret. */
export function hubKey(env: Env): Promise<KeyPair> {
  let p = keyCache.get(env);
  if (!p) {
    p = (async () => {
      let seed: Uint8Array;
      try {
        seed = b64.decode((env.HUB_SIGNING_SEED ?? "").trim());
      } catch {
        throw new ConfigError("HUB_SIGNING_SEED must be base64url of 32 bytes");
      }
      if (seed.length !== 32) throw new ConfigError("HUB_SIGNING_SEED must be base64url of 32 bytes");
      return fromSeed(seed, "sign", loadConfig(env).keyCreated);
    })();
    keyCache.set(env, p);
  }
  return p;
}

/** `release_tuple`: "0.9.0" -> [0, 9, 0]; anything odd sorts lowest. */
export function releaseTuple(s: unknown): number[] {
  const parts = String(s).split("-")[0].split("+")[0].split(".");
  const out = parts.map((x) => (/^[0-9]+$/.test(x) ? Number(x) : NaN));
  return out.some(Number.isNaN) ? [0] : out;
}

export function releaseLess(a: unknown, b: unknown): boolean {
  const x = releaseTuple(a);
  const y = releaseTuple(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const p = x[i] ?? -1; // a shorter tuple sorts first, like Python's tuple comparison
    const q = y[i] ?? -1;
    if (p !== q) return p < q;
  }
  return false;
}
