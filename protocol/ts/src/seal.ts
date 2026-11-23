// HPKE (RFC 9180) base mode for envelope bodies (messages.md "Sealing").
//
// Suite: DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20Poly1305, i.e. KEM 0x0020, KDF 0x0001, AEAD 0x0003.
// Written out from X25519, HMAC and ChaCha20Poly1305 primitives (@noble) rather than a library's HPKE so the
// ephemeral key can be fixed for test vectors; checked against RFC 9180 A.2.1 in the tests and against the Python
// reference library. Everything private stays a raw 32-byte X25519 key. Mirrors protocol/py/src/l0wproto/seal.py.
import { chacha20poly1305 } from "@noble/ciphers/chacha.js";
import { x25519 } from "@noble/curves/ed25519.js";
import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import * as b64 from "./b64";
import { SealError } from "./errors";

export const ALG = "HPKE-v1-X25519-SHA256-ChaCha20Poly1305";
export const KEM_ID = 0x0020;
export const KDF_ID = 0x0001;
export const AEAD_ID = 0x0003;
const NH = 32;
const NK = 32;
const NN = 12;
const NPK = 32;

const be16 = (n: number): Uint8Array => Uint8Array.of((n >> 8) & 0xff, n & 0xff);
const SUITE_KEM = b64.concat(b64.utf8("KEM"), be16(KEM_ID));
const SUITE = b64.concat(b64.utf8("HPKE"), be16(KEM_ID), be16(KDF_ID), be16(AEAD_ID));
const HPKE_V1 = b64.utf8("HPKE-v1");

export interface Sealed {
  alg: string;
  enc: string;
  ct: string;
}

/** `"l0wagent-seal/1\n" + from + "\n" + from_kid + "\n" + to + "\n" + to_kid + "\n" + id`, ASCII. */
export function sealInfo(fromAddr: string, fromKid: string, toAddr: string, toKid: string, envId: string): Uint8Array {
  const s = `l0wagent-seal/1\n${fromAddr}\n${fromKid}\n${toAddr}\n${toKid}\n${envId}`;
  // eslint-disable-next-line no-control-regex
  if (!/^[\x00-\x7f]*$/.test(s)) throw new SealError("info must be ASCII");
  return b64.utf8(s);
}

// --- RFC 9180 pieces ---------------------------------------------------------------------------

function extract(salt: Uint8Array, ikm: Uint8Array): Uint8Array {
  return hmac(sha256, salt.length ? salt : new Uint8Array(NH), ikm);
}

function expand(prk: Uint8Array, info: Uint8Array, length: number): Uint8Array {
  const out = new Uint8Array(Math.ceil(length / NH) * NH);
  let t: Uint8Array = new Uint8Array(0);
  for (let i = 1; i <= Math.ceil(length / NH); i++) {
    t = hmac(sha256, prk, b64.concat(t, info, Uint8Array.of(i)));
    out.set(t, (i - 1) * NH);
  }
  return out.slice(0, length);
}

const lextract = (suite: Uint8Array, salt: Uint8Array, label: string, ikm: Uint8Array): Uint8Array =>
  extract(salt, b64.concat(HPKE_V1, suite, b64.utf8(label), ikm));

const lexpand = (suite: Uint8Array, prk: Uint8Array, label: string, info: Uint8Array, length: number): Uint8Array =>
  expand(prk, b64.concat(be16(length), HPKE_V1, suite, b64.utf8(label), info), length);

const EMPTY = new Uint8Array(0);

/** The X25519 public key of a raw private key. */
export function publicKey(priv: Uint8Array): Uint8Array {
  try {
    return x25519.getPublicKey(priv);
  } catch (e) {
    throw new SealError("bad private key");
  }
}

/** RFC 9180 DeriveKeyPair for X25519 -> [private, public], both raw 32 bytes. */
export function deriveKeyPair(ikm: Uint8Array): [Uint8Array, Uint8Array] {
  const prk = lextract(SUITE_KEM, EMPTY, "dkp_prk", ikm);
  const sk = lexpand(SUITE_KEM, prk, "sk", EMPTY, 32);
  return [sk, publicKey(sk)];
}

export function dh(priv: Uint8Array, pub: Uint8Array): Uint8Array {
  let out: Uint8Array;
  try {
    out = x25519.getSharedSecret(priv, pub);
  } catch {
    throw new SealError("key agreement failed");
  }
  if (out.every((b) => b === 0)) throw new SealError("key agreement failed"); // a low-order point
  return out;
}

export function sharedSecret(dhOut: Uint8Array, enc: Uint8Array, pkR: Uint8Array): Uint8Array {
  const eaePrk = lextract(SUITE_KEM, EMPTY, "eae_prk", dhOut);
  return lexpand(SUITE_KEM, eaePrk, "shared_secret", b64.concat(enc, pkR), NH);
}

/** Base mode (0), no psk. Returns [key, baseNonce]. */
export function keySchedule(shared: Uint8Array, info: Uint8Array): [Uint8Array, Uint8Array] {
  const pskIdHash = lextract(SUITE, EMPTY, "psk_id_hash", EMPTY);
  const infoHash = lextract(SUITE, EMPTY, "info_hash", info);
  const ctx = b64.concat(Uint8Array.of(0), pskIdHash, infoHash);
  const secret = lextract(SUITE, shared, "secret", EMPTY);
  return [lexpand(SUITE, secret, "key", ctx, NK), lexpand(SUITE, secret, "base_nonce", ctx, NN)];
}

/** Single-shot base-mode seal (sequence number 0) -> [enc, ct]. `ephPriv` is for test vectors only. */
export function hpkeSeal(
  pkR: Uint8Array,
  info: Uint8Array,
  aad: Uint8Array,
  plaintext: Uint8Array,
  opts: { ephPriv?: Uint8Array } = {},
): [Uint8Array, Uint8Array] {
  if (pkR.length !== NPK) throw new SealError("recipient key must be 32 bytes");
  const skE = opts.ephPriv ?? crypto.getRandomValues(new Uint8Array(32));
  const enc = publicKey(skE);
  const shared = sharedSecret(dh(skE, pkR), enc, pkR);
  const [key, nonce] = keySchedule(shared, info);
  return [enc, chacha20poly1305(key, nonce, aad).encrypt(plaintext)];
}

export function hpkeOpen(skR: Uint8Array, enc: Uint8Array, info: Uint8Array, aad: Uint8Array, ct: Uint8Array): Uint8Array {
  if (enc.length !== NPK || skR.length !== 32) throw new SealError("cannot open");
  const pkR = publicKey(skR);
  const shared = sharedSecret(dh(skR, enc), enc, pkR);
  const [key, nonce] = keySchedule(shared, info);
  try {
    return chacha20poly1305(key, nonce, aad).decrypt(ct);
  } catch {
    throw new SealError("cannot open");
  }
}

// --- the document form -------------------------------------------------------------------------

/** -> `{alg, enc, ct}` (base64url). aad is empty. */
export function seal(recipientPubRaw: Uint8Array, plaintext: Uint8Array, info: Uint8Array, opts: { ephPriv?: Uint8Array } = {}): Sealed {
  const [enc, ct] = hpkeSeal(recipientPubRaw, info, EMPTY, plaintext, opts);
  return { alg: ALG, enc: b64.encode(enc), ct: b64.encode(ct) };
}

/** Throws SealError on any failure (unknown alg, bad encoding, wrong key, wrong info, tampering). */
export function openSealed(recipientPriv: Uint8Array, sealed: unknown, info: Uint8Array): Uint8Array {
  if (sealed === null || typeof sealed !== "object" || Array.isArray(sealed) || (sealed as Sealed).alg !== ALG) {
    throw new SealError("cannot open");
  }
  let enc: Uint8Array;
  let ct: Uint8Array;
  try {
    enc = b64.decode((sealed as Sealed).enc);
    ct = b64.decode((sealed as Sealed).ct);
  } catch {
    throw new SealError("cannot open");
  }
  return hpkeOpen(recipientPriv, enc, info, EMPTY, ct);
}
