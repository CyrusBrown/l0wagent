// base64url, no padding, strict decode (signing.md).
import { EncodingError } from "./errors";

const ALPHABET = /^[A-Za-z0-9_-]*$/;

export function encode(data: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < data.length; i += 0x8000) {
    bin += String.fromCharCode(...data.subarray(i, i + 0x8000));
  }
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Reject padding, characters outside the alphabet, and non-canonical trailing bits. */
export function decode(s: unknown): Uint8Array {
  if (typeof s !== "string") throw new EncodingError("base64url value must be a string");
  if (!ALPHABET.test(s)) throw new EncodingError("invalid base64url character (padding is not allowed)");
  if (s.length % 4 === 1) throw new EncodingError("invalid base64url length");
  const std = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(std);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  if (encode(out) !== s) throw new EncodingError("non-canonical base64url");
  return out;
}

const enc = new TextEncoder();
export const utf8 = (s: string): Uint8Array => enc.encode(s);
const dec = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
export const fromUtf8 = (b: Uint8Array): string => dec.decode(b);

export function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

export function toHex(b: Uint8Array): string {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
export function fromHex(h: string): Uint8Array {
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return out;
}
