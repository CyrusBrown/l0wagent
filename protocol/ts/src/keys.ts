// Keys and key ids (signing.md). Ed25519 through WebCrypto; X25519 (seal) keys through @noble/curves.
import { x25519 } from "@noble/curves/ed25519.js";
import * as b64 from "./b64";
import { KeyFormatError } from "./errors";
import { formatTime, now as nowEpoch } from "./times";

export const USES: Record<string, string> = { sign: "Ed25519", recover: "Ed25519", seal: "X25519" };
const ALGS = new Set(Object.values(USES));

export interface KeyJson {
  kid: string;
  alg: string;
  use: string;
  pub: string;
  created: string;
}

/** `"k1." + base64url(first 16 bytes of SHA-256(alg + "\n" + raw public key))`. */
export async function computeKid(alg: string, pub: Uint8Array): Promise<string> {
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", b64.concat(b64.utf8(alg + "\n"), pub)));
  return "k1." + b64.encode(h.subarray(0, 16));
}

export class Key {
  constructor(
    readonly kid: string,
    readonly alg: string,
    readonly use: string,
    readonly pub: Uint8Array,
    readonly created: string,
  ) {}

  /** Unknown algs are unusable, not errors (signing.md). */
  get usable(): boolean {
    return this.alg === "Ed25519" || this.alg === "X25519";
  }

  toJSON(): KeyJson {
    return { kid: this.kid, alg: this.alg, use: this.use, pub: b64.encode(this.pub), created: this.created };
  }

  /** Parse and verify the kid. Throws KeyFormatError on mismatch. */
  static async fromJSON(d: unknown): Promise<Key> {
    if (d === null || typeof d !== "object" || Array.isArray(d)) throw new KeyFormatError("key must be an object");
    const o = d as Record<string, unknown>;
    for (const f of ["kid", "alg", "use", "pub", "created"]) {
      if (typeof o[f] !== "string") throw new KeyFormatError(`key field '${f}' missing or not a string`);
    }
    const pub = b64.decode(o.pub);
    if (ALGS.has(o.alg as string) && pub.length !== 32) throw new KeyFormatError("public key must be 32 bytes");
    const key = new Key(o.kid as string, o.alg as string, o.use as string, pub, o.created as string);
    if (key.kid !== (await computeKid(key.alg, key.pub))) throw new KeyFormatError("kid does not match the public key");
    return key;
  }

  async verify(signature: Uint8Array, message: Uint8Array): Promise<boolean> {
    if (this.alg !== "Ed25519") return false;
    try {
      const k = await crypto.subtle.importKey("raw", this.pub, { name: "Ed25519" }, false, ["verify"]);
      return await crypto.subtle.verify({ name: "Ed25519" }, k, signature, message);
    } catch {
      return false;
    }
  }
}

/** True when the kid matches the key's algorithm and public bytes. */
export async function verifyKid(key: Key): Promise<boolean> {
  return key.kid === (await computeKid(key.alg, key.pub));
}

/** A key with its private half (held by the gateway; test tools and fake hubs). `private` is the raw 32-byte
 * seed (Ed25519) or X25519 private key. Only Ed25519 keys sign. */
export class KeyPair {
  constructor(
    readonly key: Key,
    private readonly seed: Uint8Array,
    private readonly signer: CryptoKey | null = null,
  ) {}
  /** The raw 32-byte private key (Ed25519 seed or X25519 key). */
  get private(): Uint8Array {
    return this.seed;
  }
  get kid(): string {
    return this.key.kid;
  }
  async sign(message: Uint8Array): Promise<Uint8Array> {
    if (this.key.alg !== "Ed25519" || this.signer === null) throw new KeyFormatError("only Ed25519 keys sign");
    return new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, this.signer, message));
  }
}

// PKCS#8 wrapper for a raw 32-byte Ed25519 seed (RFC 8410).
const PKCS8_PREFIX = Uint8Array.from([0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20]);

export async function fromSeed(seed: Uint8Array, use = "sign", created = "2026-11-17T21:30:00Z"): Promise<KeyPair> {
  if (seed.length !== 32) throw new KeyFormatError("seed must be 32 bytes");
  if (!(use in USES)) throw new KeyFormatError(`unknown use '${use}'`);
  const alg = USES[use];
  if (alg === "X25519") {
    const pub = x25519.getPublicKey(seed);
    return new KeyPair(new Key(await computeKid(alg, pub), alg, use, pub, created), Uint8Array.from(seed));
  }
  const priv = await crypto.subtle.importKey("pkcs8", b64.concat(PKCS8_PREFIX, seed), { name: "Ed25519" }, true, ["sign"]);
  const jwk = (await crypto.subtle.exportKey("jwk", priv)) as JsonWebKey;
  if (!jwk.x) throw new KeyFormatError("could not derive the public key");
  const pub = b64.decode(jwk.x);
  const key = new Key(await computeKid("Ed25519", pub), "Ed25519", use, pub, created);
  return new KeyPair(key, Uint8Array.from(seed), priv);
}

export async function generate(use = "sign", created?: string): Promise<KeyPair> {
  return fromSeed(crypto.getRandomValues(new Uint8Array(32)), use, created ?? formatTime(nowEpoch()));
}
