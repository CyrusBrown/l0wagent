import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as b64 from "../src/proto/b64";
import { type KeyPair, fromSeed } from "../src/proto/keys";

const VEC = join(import.meta.dirname, "..", "..", "protocol", "vectors") + "/";

/* eslint-disable @typescript-eslint/no-explicit-any */
export const vec = (name: string): any => JSON.parse(readFileSync(VEC + name + ".json", "utf8"));

export async function vectorKey(label: string): Promise<KeyPair> {
  const k = vec("keys").keys[label];
  return fromSeed(b64.fromHex(k.seed), k.key.use, k.key.created);
}

export const parseNow = (s: string): number => Date.parse(s) / 1000;
