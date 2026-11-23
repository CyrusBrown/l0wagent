import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as b64 from "../src/b64";
import { type KeyPair, fromSeed } from "../src/keys";

const VEC = join(import.meta.dirname, "..", "..", "vectors") + "/";

/* eslint-disable @typescript-eslint/no-explicit-any */
export const vec = (name: string): any => JSON.parse(readFileSync(VEC + name + ".json", "utf8"));

export async function vectorKey(label: string): Promise<KeyPair> {
  const k = vec("keys").keys[label];
  return fromSeed(b64.fromHex(k.seed), k.key.use, k.key.created);
}

export const parseNow = (s: string): number => Date.parse(s) / 1000;

import { sha256 } from "@noble/hashes/sha2.js";

export const IAT = "2026-11-17T21:30:00Z";
export const NOW = Date.parse(IAT) / 1000;
export const HUB = "hub.example";
export const ADDR = `mara@${HUB}`;

/** A key pair from a seed made by hashing a label (the Python tests' conftest does the same). */
export const labelled = (label: string, use: string): Promise<KeyPair> =>
  fromSeed(sha256(new TextEncoder().encode(label)), use, IAT);

/** Calls `fn` and returns the error it throws (fails the test if it does not). */
export async function thrown(fn: () => unknown): Promise<any> {
  try {
    await fn();
  } catch (e) {
    return e;
  }
  throw new Error("expected the call to throw");
}
