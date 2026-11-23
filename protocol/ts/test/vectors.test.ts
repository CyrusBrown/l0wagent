import { describe, expect, it } from "vitest";
import * as b64 from "../src/b64";
import * as docs from "../src/docs";
import * as P from "../src/payloads";
import { Key, fromSeed } from "../src/keys";
import { checkName, lookalikeKey, validateName } from "../src/names";
import { parseInvite } from "../src/invite";
import * as lt from "../src/tunnel";
import { parseNow, vec, vectorKey } from "./util";

const DOC_VECTORS = [
  "rotation",
  "rotation-recovery",
  "self",
  "self-after-rotation",
  "revocation",
  "moved",
  "hub",
  "registration",
  "leave",
  "reregistration",
  "tunnel-hello",
  "accelerator-request",
  "service",
  "role-grant",
];

describe("base64url vectors", () => {
  const b = vec("base64url");
  it("encodes and decodes the valid cases", () => {
    for (const c of b.valid) {
      expect(b64.encode(b64.fromHex(c.bytes_hex))).toBe(c.b64);
      expect(b64.toHex(b64.decode(c.b64))).toBe(c.bytes_hex);
    }
  });
  it("rejects the invalid cases", () => {
    for (const c of b.invalid) expect(() => b64.decode(c.b64), c.why).toThrow();
  });
});

describe("keys vector", () => {
  it("reproduces every key (and kid) from its seed", async () => {
    for (const [label, k] of Object.entries<any>(vec("keys").keys)) {
      if (k.key.alg !== "Ed25519") {
        // X25519 keys are only parsed and kid-checked, never used
        expect((await Key.fromJSON(k.key)).toJSON()).toEqual(k.key);
        continue;
      }
      const kp = await fromSeed(b64.fromHex(k.seed), k.key.use, k.key.created);
      expect(kp.key.toJSON(), label).toEqual(k.key);
    }
  });
  it("refuses a key whose kid does not match", async () => {
    const k = vec("keys").keys["hub-sign"].key;
    await expect(Key.fromJSON({ ...k, kid: "k1.AAAAAAAAAAAAAAAAAAAAAA" })).rejects.toThrow();
    await expect(Key.fromJSON({ ...k, pub: b64.encode(new Uint8Array(31)) })).rejects.toThrow();
  });
});

describe.each(DOC_VECTORS)("document vector %s", (name) => {
  it("verifies byte for byte and typed, and tampering fails", async () => {
    const v = vec(name);
    const now = parseNow(v.now);
    const doc = v.document;
    expect(doc.payload).toBe(v.payload_b64);
    expect(doc.payload).toBe(b64.encode(b64.utf8(v.payload_json)));
    expect(new TextDecoder().decode(docs.signingInput(v.type, v.payload_b64))).toBe(v.signing_input);
    expect(doc.sigs).toEqual(v.signatures);
    const ks = await Promise.all(v.verify_keys.map((k: any) => Key.fromJSON(k)));
    const payload = await docs.verifyDocument(doc, ks, { expectedType: v.type, required: v.required, now });
    expect(payload).toEqual(JSON.parse(v.payload_json));
    const memberSign = (await vectorKey("member-sign")).key;
    const memberRecover = (await vectorKey("member-recover")).key;
    const typed: Record<string, () => Promise<unknown>> = {
      self: () => P.verifySelf(doc, now),
      "self-after-rotation": () => P.verifySelf(doc, now),
      hub: () => P.verifyHub(doc, ks[0], now),
      registration: () => P.verifyRegistration(doc, "hub.example", now),
      moved: () => P.verifyMoved(doc, ks[0], now),
      leave: () => P.verifyLeave(doc, ks[0], now),
      reregistration: () => P.verifyReregistration(doc, ks[0], now),
      revocation: async () => (await docs.verifyOneOf(doc, ks, { expectedType: "revocation", now }))[0],
      rotation: () => P.verifyRotation(doc, memberSign, memberRecover, now),
      "rotation-recovery": () => P.verifyRotation(doc, memberSign, memberRecover, now),
      "accelerator-request": () => P.verifyAcceleratorRequest(doc, ks[0], { hub: "hub.example", address: "mara@hub.example", now }),
      service: () => P.verifyService(doc, ks[0], { hubDomain: "hub.example", now }),
      "role-grant": () => P.verifyRoleGrant(doc, ks[0], { service: "board@hub.example", holder: "mara@hub.example", now }),
      "tunnel-hello": () => P.verifyTunnelHello(doc, ks[0], { nonce: v.challenge.nonce, hub: "hub.example", now }),
    };
    expect(await typed[name]()).toEqual(payload);
    const bad = JSON.parse(JSON.stringify(doc));
    const raw = b64.decode(bad.payload);
    raw[10] ^= 1;
    bad.payload = b64.encode(raw);
    await expect(docs.verifyDocument(bad, ks, { now })).rejects.toThrow();
  });
});

describe("builders reproduce the vectors' payload bytes", () => {
  it("hub", async () => {
    const v = vec("hub");
    const p = JSON.parse(v.payload_json);
    const built = P.buildHub({
      name: p.name,
      domain: p.domain,
      keys: p.keys,
      api: p.api,
      tunnel: p.tunnel,
      minRelease: p.instance_releases.min,
      membership: p.membership,
      hostnames: p.hostnames,
      policies: p.policies,
      limits: p.limits,
      extras: p.extras,
      iat: p.iat,
    });
    expect(new TextDecoder().decode(docs.serializePayload(built))).toBe(v.payload_json);
    const signed = await docs.signDocument("hub", built, [await vectorKey("hub-sign")]);
    expect(signed).toEqual(v.document);
  });
  it("reregistration and revocation", async () => {
    const hub = await vectorKey("hub-sign");
    const rr = vec("reregistration");
    const p = JSON.parse(rr.payload_json);
    const built = P.buildReregistration(p.address, p.old_keys, p.new_keys, p.iat);
    expect(await docs.signDocument("reregistration", built, [hub])).toEqual(rr.document);
    const rv = vec("revocation");
    const q = JSON.parse(rv.payload_json);
    expect(new TextDecoder().decode(docs.serializePayload(P.buildRevocation(q.address, q.kid, q.reason, q.iat)))).toBe(rv.payload_json);
  });
});

describe("directory entry vector", () => {
  it("verifies, and the hub countersignature is reproduced", async () => {
    const v = vec("directory-entry");
    const hub = await Key.fromJSON(v.hub_key);
    const now = parseNow(v.now);
    const p = await P.verifyDirectoryEntry(v.entry, hub, { hubDomain: v.hub_domain, name: v.name, now });
    expect(p).toEqual(JSON.parse(vec("self").payload_json));
    expect(new TextDecoder().decode(docs.signingInput("directory-entry", v.entry.payload))).toBe(v.hub_signing_input);
    const countersigned = await docs.countersignDirectory(vec("self").document, await vectorKey("hub-sign"));
    expect(countersigned).toEqual(v.entry);
    await expect(P.verifyDirectoryEntry(v.entry, hub, { hubDomain: "other.example", now })).rejects.toThrow();
    await expect(P.verifyDirectoryEntry(v.entry, hub, { hubDomain: v.hub_domain, name: "someone", now })).rejects.toThrow();
  });
  it("a member self document is not a directory entry", async () => {
    const v = vec("directory-entry");
    const hub = await Key.fromJSON(v.hub_key);
    await expect(P.verifyDirectoryEntry(vec("self").document, hub, { hubDomain: v.hub_domain, now: parseNow(v.now) })).rejects.toThrow();
  });
});

describe("names and invites vectors", () => {
  const n = vec("names");
  it("syntax", () => {
    for (const x of n.valid) validateName(x);
    for (const x of n.invalid) expect(() => validateName(x), x).toThrow();
  });
  it("reserved and lookalike", async () => {
    const none = { isTaken: async () => false, lookalikeTaken: async () => false };
    for (const x of n.reserved) await expect(checkName(x, none)).rejects.toMatchObject({ code: "name_reserved" });
    for (const c of n.lookalike) {
      const existing: string | null = c.existing_or_reserved;
      const checks = {
        isTaken: async (name: string) => name === existing,
        lookalikeTaken: async (key: string) => existing !== null && lookalikeKey(existing) === key,
      };
      if (existing) await expect(checkName(c.candidate, checks)).rejects.toThrow();
      else await checkName(c.candidate, { isTaken: async (x) => x === "mara", lookalikeTaken: async (k) => k === lookalikeKey("mara") });
    }
  });
  it("invites", () => {
    const i = vec("invites");
    for (const c of i.valid) {
      const p = parseInvite(c.string);
      expect([p.baseUrl, p.code]).toEqual([c.base_url, c.code]);
    }
    for (const s of i.invalid) expect(() => parseInvite(s), s).toThrow();
  });
});

describe("tunnel frame vectors", () => {
  const v = vec("tunnel-frames");
  it("decodes and re-encodes the valid frames", () => {
    for (const c of v.valid) {
      if (c.elided) continue;
      const raw = b64.fromHex(c.frame_hex);
      const f = lt.decode(raw);
      expect([f.type, f.flags, f.streamId, b64.toHex(f.payload)]).toEqual([c.type, c.flags, c.stream_id, c.payload_hex]);
      expect(b64.toHex(lt.encode(f))).toBe(c.frame_hex);
    }
  });
  it("rejects the malformed frames", () => {
    for (const c of v.malformed) {
      if (c.frame_hex_elided) continue;
      expect(() => lt.decode(b64.fromHex(c.frame_hex)), c.why).toThrow();
    }
  });
});
