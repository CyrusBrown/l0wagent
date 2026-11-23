import { describe, expect, it } from "vitest";
import * as b64 from "../src/b64";
import { ENVELOPE_MAX_AGE, MAX_FILES } from "../src/consts";
import * as docs from "../src/docs";
import { ProtocolError, SealError, SignatureError, ValidationError } from "../src/errors";
import { Key, fromSeed } from "../src/keys";
import * as M from "../src/messages";
import * as P from "../src/payloads";
import * as S from "../src/seal";
import { parseTime } from "../src/times";
import { ADDR, IAT, NOW, labelled, parseNow, thrown, vec } from "./util";

const BOB = "bob@other.example";
const dec = (d: any) => JSON.parse(b64.fromUtf8(b64.decode(d.payload)));
const KID_A = "k1." + "A".repeat(22);
const KID_B = "k1." + "B".repeat(22);

async function world() {
  const mara = await labelled("a", "sign");
  const bobSign = await labelled("bob-sign", "sign");
  const bobSeal = await labelled("bob-seal", "seal");
  const tok = await M.issueDeliveryToken(bobSign, BOB, ADDR, mara.kid, { iat: IAT });
  const reqTok = await M.issueDeliveryToken(mara, ADDR, BOB, bobSign.kid, { iat: IAT });
  return { mara, bobSign, bobSeal, tok, reqTok };
}
type W = Awaited<ReturnType<typeof world>>;

const msgBody = (o: any = {}) => ({ thread: "t1", reply_to: null, text: "hi", files: [], ...o });

async function send(w: W, kind = "message", body: any = msgBody(), token: any = "default", o: any = {}) {
  const t = token === "default" ? w.tok : token;
  return M.makeEnvelope(w.mara, w.mara.kid, ADDR, BOB, w.bobSeal.key, kind, body, o.seq ?? 1, t, { iat: o.iat ?? IAT, ephPriv: o.ephPriv });
}

const receive = (w: W, doc: any, o: { now?: number; senderSignKey?: Key } = {}) =>
  M.openEnvelope(doc, {
    myAddress: BOB,
    mySealKeys: new Map([[w.bobSeal.kid, w.bobSeal.private]]),
    senderSignKey: o.senderSignKey ?? w.mara.key,
    now: o.now ?? NOW,
  });

describe("envelopes", () => {
  it("round-trips, and the plaintext never shows in the outer document", async () => {
    const w = await world();
    const doc = await send(w, "message", msgBody({ text: "Hello é" }));
    const [p, body] = await receive(w, doc);
    expect(body.text).toBe("Hello é");
    expect(p.kind).toBe("message");
    expect(p.seq).toBe(1);
    expect(M.tokenId(doc)).toBe(M.tokenId(w.tok));
    expect(M.tokenId(w.tok)).toBe(dec(w.tok).id);
    expect(b64.fromUtf8(b64.decode(doc.payload))).not.toContain("Hello");
    const tp = await M.verifyDeliveryToken(w.tok, w.bobSign.key, w.mara.kid, NOW);
    expect(tp.holder).toBe(ADDR);
    await expect(M.verifyDeliveryToken(w.tok, w.bobSign.key, "k1.AAAAAAAAAAAAAAAAAAAAAA", NOW)).rejects.toThrow(SignatureError);
    await expect(M.verifyDeliveryToken(w.tok, w.mara.key, undefined, NOW)).rejects.toThrow(SignatureError);
  });

  it("wrong recipient key or unknown kid is bad_seal", async () => {
    const w = await world();
    const doc = await send(w);
    const other = await labelled("x", "seal");
    const e = await thrown(() =>
      M.openEnvelope(doc, { myAddress: BOB, mySealKeys: { [w.bobSeal.kid]: other.private }, senderSignKey: w.mara.key, now: NOW }),
    );
    expect(e).toBeInstanceOf(SealError);
    expect(e.code).toBe("bad_seal");
    await expect(M.openEnvelope(doc, { myAddress: BOB, mySealKeys: {}, senderSignKey: w.mara.key, now: NOW })).rejects.toThrow(SealError);
  });

  it("another address is not_here", async () => {
    const w = await world();
    const e = await thrown(async () =>
      M.openEnvelope(await send(w), { myAddress: "carol@other.example", mySealKeys: {}, senderSignKey: w.mara.key, now: NOW }),
    );
    expect(e).toBeInstanceOf(ProtocolError);
    expect(e.code).toBe("not_here");
  });

  it("a body re-wrapped by another sender does not open", async () => {
    const w = await world();
    const eve = await labelled("f", "sign");
    const doc = await send(w);
    const p = dec(doc);
    await expect(receive(w, await docs.signDocument("envelope", { ...p, from: "eve@hub.example", from_kid: eve.kid }, [eve]), { senderSignKey: eve.key })).rejects.toThrow(SealError);
    await expect(receive(w, await docs.signDocument("envelope", { ...p, from_kid: eve.kid }, [eve]), { senderSignKey: eve.key })).rejects.toThrow(SealError);
    await expect(receive(w, doc, { senderSignKey: eve.key })).rejects.toThrow(SignatureError);
    const bad = JSON.parse(JSON.stringify(doc));
    bad.sigs[0].sig = b64.encode(new Uint8Array(64));
    await expect(receive(w, bad)).rejects.toThrow(SignatureError);
  });

  it("tampered sealed body, id or alg fails", async () => {
    const w = await world();
    const doc = await send(w);
    const resign = (p: any) => docs.signDocument("envelope", p, [w.mara]);
    let p = dec(doc);
    const ct = b64.decode(p.sealed.ct);
    ct[0] ^= 1;
    p.sealed.ct = b64.encode(ct);
    await expect(receive(w, await resign(p))).rejects.toThrow(SealError);
    p = dec(doc);
    p.id = P.newId();
    await expect(receive(w, await resign(p))).rejects.toThrow(SealError);
    p = dec(doc);
    p.sealed.alg = "HPKE-v2";
    await expect(receive(w, await resign(p))).rejects.toThrow(SealError);
  });

  it("token rules", async () => {
    const w = await world();
    await expect(send(w, "message", msgBody(), null)).rejects.toThrow(ValidationError);
    const fr = { code: null, note: "hi", by: "owner", token: w.reqTok };
    await expect(send(w, "friend-request", fr, w.tok)).rejects.toThrow(ValidationError);
    const doc = await send(w, "friend-request", fr, null);
    const [p, body] = await receive(w, doc);
    expect("token" in p).toBe(false);
    expect(M.tokenId(doc)).toBeNull();
    expect(body.by).toBe("owner");
    const so = await docs.signDocument("same-owner", P.buildSameOwner(ADDR, KID_A, BOB, KID_B, IAT), [w.mara]);
    await expect(send(w, "message", msgBody(), so)).rejects.toThrow(ValidationError);
    const q = dec(doc);
    q.token = w.tok;
    expect(() => M.envelopePayload(null)).toThrow();
    await expect(docs.signDocument("envelope", q, [w.mara]).then((d) => M.envelopePayload(d))).rejects.toThrow(ValidationError);
  });

  it("checks the iat window", async () => {
    const w = await world();
    await expect(receive(w, await send(w, "message", msgBody(), "default", { iat: NOW - ENVELOPE_MAX_AGE - 1 }))).rejects.toThrow(ValidationError);
    await receive(w, await send(w, "message", msgBody(), "default", { iat: NOW - ENVELOPE_MAX_AGE }));
    await expect(receive(w, await send(w, "message", msgBody(), "default", { iat: NOW + 301 }))).rejects.toThrow(ValidationError);
    await receive(w, await send(w, "message", msgBody(), "default", { iat: NOW + 300 }));
    await receive(w, await send(w), { now: NOW + 7 * 86400 });
  });

  it("unknown kind keeps its code", async () => {
    const w = await world();
    const p = dec(await send(w));
    p.kind = "carrier-pigeon";
    const e = await thrown(async () => receive(w, await docs.signDocument("envelope", p, [w.mara])));
    expect(e).toBeInstanceOf(ValidationError);
    expect(e.code).toBe("unknown_kind");
    expect(() => P.validateBody("carrier-pigeon", {})).toThrow(expect.objectContaining({ code: "unknown_kind" }));
  });

  it("a sealed body that does not match its kind is bad_body", async () => {
    const w = await world();
    const p = dec(await send(w));
    const info = S.sealInfo(p.from, p.from_kid, p.to, p.to_kid, p.id);
    for (const plain of ['{"nope":1}', "not json", "[1]"]) {
      p.sealed = S.seal(w.bobSeal.key.pub, b64.utf8(plain), info);
      const e = await thrown(async () => receive(w, await docs.signDocument("envelope", p, [w.mara])));
      expect(e).toBeInstanceOf(ValidationError);
      expect(e.code).toBe("bad_body");
    }
  });

  it("envelope shape rules", async () => {
    const w = await world();
    const p = dec(await send(w));
    P.validateEnvelope(p);
    for (const mut of [{ seq: 0 }, { id: "short" }, { to: "nope" }, { to_kid: "x" }, { sealed: { alg: "x" } }, { kind: 3 }]) {
      expect(() => P.validateEnvelope({ ...p, ...mut }), JSON.stringify(mut)).toThrow(ValidationError);
    }
    const { token: _t, ...noToken } = p;
    expect(() => P.validateEnvelope(noToken)).toThrow(ValidationError);
  });

  it("fits 5 MiB of files in an envelope, and refuses more", async () => {
    const w = await world();
    const big = (n: number) => P.buildFile("a", "text/plain", new Uint8Array(n).fill(1));
    const doc = await send(w, "message", msgBody({ files: [big(MAX_FILES)] }));
    expect(JSON.stringify(doc).length).toBeLessThan(16 * 1024 * 1024);
    const [, body] = await receive(w, doc);
    expect(body.files[0].size).toBe(MAX_FILES);
    await expect(send(w, "message", msgBody({ files: [big(MAX_FILES + 1)] }))).rejects.toThrow(ValidationError);
  });

  it("makeEnvelope refuses a mismatched sender kid or a non-seal recipient key", async () => {
    const w = await world();
    await expect(M.makeEnvelope(w.mara, w.bobSign.kid, ADDR, BOB, w.bobSeal.key, "message", msgBody(), 1, w.tok)).rejects.toThrow(ValidationError);
    await expect(M.makeEnvelope(w.mara, w.mara.kid, ADDR, BOB, w.bobSign.key, "message", msgBody(), 1, w.tok)).rejects.toThrow(ValidationError);
  });
});

const file = (name = "a.txt", data = "abc") => P.buildFile(name, "text/plain", b64.utf8(data));

describe("bodies", () => {
  it("validates every kind", async () => {
    const w = await world();
    const tok = w.tok;
    const ok: Record<string, any> = {
      "friend-request": { code: "abc", note: "n", by: "assistant", token: tok },
      "friend-accept": { request: "r", token: tok },
      "friend-decline": { request: "r" },
      unfriend: {},
      message: msgBody({ reply_to: "x", files: [file()] }),
      presence: { state: "idle", next: "tonight", now: null },
      "wake-request": { reason: "r" },
      "same-owner": { statement: await docs.signDocument("same-owner", P.buildSameOwner(ADDR, KID_A, BOB, KID_B, IAT), [w.mara]) },
      "wake-reply": { request: "r", outcome: "later" },
    };
    expect(Object.keys(ok).sort()).toEqual(P.KINDS.filter((k) => !P.SERVICE_KINDS.includes(k as any)).sort());
    for (const [k, b] of Object.entries(ok)) {
      expect(P.validateBody(k, b)).toBe(b);
      expect(() => P.validateBody(k, [b])).toThrow(expect.objectContaining({ code: "bad_body" }));
    }
    P.validateBody("presence", { state: "asleep", next: "2026-11-18T08:00:00Z", now: "x" });
    P.validateBody("presence", { state: "asleep" });
    const fr = ok["friend-request"];
    const { code: _c, ...noCode } = fr;
    const bad: Array<[string, any]> = [
      ["friend-request", { ...fr, by: "robot" }],
      ["friend-request", noCode],
      ["friend-request", { ...fr, token: { type: "x" } }],
      ["friend-request", { ...fr, note: "x".repeat(5000) }],
      ["friend-accept", { request: "r" }],
      ["friend-decline", {}],
      ["message", msgBody({ text: 5 })],
      ["message", msgBody({ files: "x" })],
      ["message", msgBody({ reply_to: 3 })],
      ["message", { text: "x" }],
      ["message", msgBody({ files: [{ ...file(), size: 99 }] })],
      ["message", msgBody({ files: [{ ...file(), sha256: "0".repeat(64) }] })],
      ["message", msgBody({ files: [file("../x")] })],
      ["message", msgBody({ files: [file("a/b")] })],
      ["presence", { state: "dead" }],
      ["presence", { state: "idle", next: "someday" }],
      ["wake-request", {}],
      ["wake-reply", { request: "r", outcome: "maybe" }],
      ["same-owner", { statement: tok }],
      ["same-owner", {}],
    ];
    for (const [k, b] of bad) expect(() => P.validateBody(k, b), k).toThrow(ValidationError);
  });

  it("limits the files in one message", () => {
    const big = new Uint8Array(3 * 1024 * 1024);
    P.validateBody("message", msgBody({ files: [P.buildFile("a", "x", big)] }));
    expect(() => P.validateBody("message", msgBody({ files: [P.buildFile("a", "x", big), P.buildFile("b", "x", big)] }))).toThrow(ValidationError);
  });

  it("same-owner needs both signatures", async () => {
    const a = await labelled("a", "sign");
    const b = await labelled("d", "sign");
    const p = P.buildSameOwner(ADDR, a.kid, "mara2@other.example", b.kid, IAT);
    const d = await docs.signDocument("same-owner", p, [a, b]);
    expect((await P.verifySameOwner(d, a.key, b.key, NOW)).b).toBe("mara2@other.example");
    for (const one of [[a], [b]]) {
      await expect(P.verifySameOwner(await docs.signDocument("same-owner", p, one), a.key, b.key, NOW)).rejects.toThrow(SignatureError);
    }
    expect(() => P.validateSameOwner(P.buildSameOwner(ADDR, a.kid, ADDR, b.kid, IAT))).toThrow(ValidationError);
  });
});

describe("fingerprints and friend codes", () => {
  it("fingerprint format", () => {
    const fp = M.fingerprint(new Uint8Array(32));
    expect(fp).toHaveLength(19);
    expect(fp.split("-")).toHaveLength(4);
    expect(M.fingerprint(new Uint8Array(32).fill(97))).not.toBe(fp);
  });

  it("round-trips a friend code and refuses bad ones", () => {
    const fp = M.fingerprint(new Uint8Array(32).fill(107));
    const [text, secret] = M.makeFriendCode("Bob@Other.Example", fp);
    expect(secret).toHaveLength(16);
    expect(text.startsWith("l0wf1:bob@other.example:")).toBe(true);
    expect(M.parseFriendCode(text)).toEqual(["bob@other.example", fp, secret]);
    const [text2] = M.makeFriendCode("bob@other.example:8443", fp.replaceAll("-", ""), secret);
    expect(M.parseFriendCode(text2)).toEqual(["bob@other.example:8443", fp, secret]);
    const flat = fp.replaceAll("-", "");
    const sec = b64.encode(secret);
    const bads = [
      "",
      "x",
      text.replace("l0wf1", "l0wf2"),
      text.slice(0, -1),
      text + "A",
      `l0wf1:bob@other.example:${flat}`,
      `l0wf1:bob@other.example:${flat.slice(0, -1)}:${sec}`,
      `l0wf1:bob@other.example:${fp}:${sec}`,
      `l0wf1:bob@other.example:U${flat.slice(1)}:${sec}`,
      `l0wf1:bob@other.example:${flat.toLowerCase()}:${sec}`,
      `l0wf1:BOB@other.example:${flat}:${sec}`,
      `l0wf1:bob:${flat}:${sec}`,
      `l0wf1:bob@other.example:${flat}:${sec}=`,
      `l0wf1:bob@other.example:${flat}:${b64.encode(new Uint8Array(15).fill(120))}`,
      `l0wf1::${flat}:${sec}`,
    ];
    for (const bad of bads) {
      expect(() => M.parseFriendCode(bad), bad).toThrow(expect.objectContaining({ code: "bad_friend_code" }));
    }
    expect(() => M.makeFriendCode("bob@other.example", "nope")).toThrow(ValidationError);
    expect(() => M.makeFriendCode("bob@other.example", fp, new Uint8Array(5))).toThrow(ValidationError);
  });
});

// --- vectors -----------------------------------------------------------------------------------

describe("envelope vector", () => {
  it("opens, and the same inputs reproduce the sealed body and the signature byte for byte", async () => {
    const v = vec("envelope");
    const now = parseNow(v.now);
    const sender = await Key.fromJSON(v.verify_keys[0]);
    const sealKey = await Key.fromJSON(v.recipient_seal_key.key);
    const priv = b64.fromHex(v.recipient_seal_key.seed);
    expect((await fromSeed(priv, "seal", sealKey.created)).key.toJSON()).toEqual(sealKey.toJSON());
    const [p, body] = await M.openEnvelope(v.document, {
      myAddress: v.recipient,
      mySealKeys: new Map([[sealKey.kid, priv]]),
      senderSignKey: sender,
      now,
    });
    expect(b64.fromUtf8(docs.serializePayload(body))).toBe(v.body_json);
    const info = S.sealInfo(p.from, p.from_kid, p.to, p.to_kid, p.id);
    expect(b64.fromUtf8(info)).toBe(v.sealed_info);
    expect(b64.toHex(info)).toBe(v.sealed_info_hex);
    expect(b64.encode(S.publicKey(b64.fromHex(v.ephemeral_seed)))).toBe(p.sealed.enc);
    expect(p.token).toEqual(vec("delivery-token").document);

    // byte-exact: seal with the vector's ephemeral key
    const sealed = S.seal(sealKey.pub, b64.utf8(v.body_json), info, { ephPriv: b64.fromHex(v.ephemeral_seed) });
    expect(sealed).toEqual(p.sealed);
    // and sign: rebuild the whole envelope from the sender's seed (member-sign) and compare the document
    const m = vec("keys").keys["member-sign"];
    const senderPair = await fromSeed(b64.fromHex(m.seed), "sign", m.key.created);
    const rebuilt = await M.makeEnvelope(senderPair, p.from_kid, p.from, p.to, sealKey, p.kind, JSON.parse(v.body_json), p.seq, p.token, {
      envId: p.id,
      iat: p.iat,
      ephPriv: b64.fromHex(v.ephemeral_seed),
    });
    expect(rebuilt).toEqual(v.document);
    expect(rebuilt.sigs).toEqual(v.signatures);
  });
});

describe("delivery-token and same-owner vectors", () => {
  it("verify, and the token is reproduced", async () => {
    const v = vec("delivery-token");
    const issuer = await Key.fromJSON(v.verify_keys[0]);
    const now = parseNow(v.now);
    const p = await M.verifyDeliveryToken(v.document, issuer, null, now);
    expect(p).toEqual(JSON.parse(v.payload_json));
    expect(M.tokenId(v.document)).toBe(p.id);
    const peer = v.peer_keys["peer-sign"];
    const pair = await fromSeed(b64.fromHex(peer.seed), "sign", peer.key.created);
    const q = JSON.parse(v.payload_json);
    const issued = await M.issueDeliveryToken(pair, q.issuer, q.holder, q.holder_kid, { id: q.id, iat: q.iat });
    expect(issued).toEqual(v.document);
    const s = vec("same-owner");
    const [ka, kb] = await Promise.all(s.verify_keys.map((k: any) => Key.fromJSON(k)));
    await P.verifySameOwner(s.document, ka, kb, parseNow(s.now));
    expect(P.validateStatementDocument(s.document)).toEqual(JSON.parse(s.payload_json));
  });
});

describe("fingerprint and friend-code vectors", () => {
  it("match", () => {
    for (const c of vec("fingerprint").cases) expect(M.fingerprint(b64.decode(c.pub))).toBe(c.fingerprint);
    const fc = vec("friend-code");
    for (const c of fc.valid) {
      const [a, fp, sec] = M.parseFriendCode(c.text);
      expect([a, fp, b64.encode(sec)]).toEqual([c.address, c.fingerprint, c.secret]);
      expect(M.makeFriendCode(a, fp, sec)[0]).toBe(c.text);
    }
    for (const t of fc.invalid) expect(() => M.parseFriendCode(t), t).toThrow(ValidationError);
  });
});

describe("service-call vector", () => {
  it("opens and the embedded role grant verifies", async () => {
    const v = vec("service-call");
    const sender = await Key.fromJSON(vec("keys").keys["member-sign"].key);
    const sealKey = await Key.fromJSON(v.recipient_seal_key.key);
    const priv = b64.fromHex(v.recipient_seal_key.seed);
    const now = parseNow(v.now);
    const [p, body] = await M.openEnvelope(v.document, { myAddress: v.recipient, mySealKeys: { [sealKey.kid]: priv }, senderSignKey: sender, now });
    expect(p.kind).toBe("service-call");
    expect(body).toEqual(JSON.parse(v.body_json));
    const serviceKey = await Key.fromJSON(vec("role-grant").service_key.key);
    const g = await P.verifyRoleGrant(body.role, serviceKey, { service: v.recipient, holder: ADDR, holderKid: sender.kid, now });
    expect(g.role).toBe("moderator");
    // byte-exact seal with the vector's ephemeral key
    const info = S.sealInfo(p.from, p.from_kid, p.to, p.to_kid, p.id);
    expect(S.seal(sealKey.pub, b64.utf8(v.body_json), info, { ephPriv: b64.fromHex(v.ephemeral_seed) })).toEqual(p.sealed);
  });
});

// --- hub services --------------------------------------------------------------------------------

const SVC = "board@hub.example";
const CALL = "AAAAAAAAAAAAAAAAAAAAAA";

async function swarld() {
  const svc = await labelled("svc-sign", "sign");
  const svcSeal = await labelled("svc-seal", "seal");
  const hub = await labelled("e", "sign");
  const member = await labelled("a", "sign");
  const other = await labelled("f", "sign");
  const mtok = await M.issueDeliveryToken(member, ADDR, SVC, svc.kid, { iat: IAT });
  const stok = await M.issueDeliveryToken(svc, SVC, ADDR, member.kid, { iat: IAT });
  const grant = await docs.signDocument("role-grant", P.buildRoleGrant(SVC, ADDR, member.kid, "moderator", IAT), [svc]);
  return { svc, svcSeal, hub, member, other, mtok, stok, grant };
}
type SW = Awaited<ReturnType<typeof swarld>>;

async function serviceDoc(w: SW, o: any = {}) {
  const { signer, address, kind, roles, ...rest } = o;
  const p = P.buildService(address ?? SVC, kind ?? "board", roles ?? ["moderator", "poster"], { iat: IAT, ...rest });
  return docs.signDocument("service", p, [signer ?? w.hub]);
}

const sendSvc = (w: SW, kind: string, body: any, token: any = "default") =>
  M.makeEnvelope(w.member, w.member.kid, ADDR, SVC, w.svcSeal.key, kind, body, 1, token === "default" ? w.mtok : token, { iat: IAT });

describe("service documents", () => {
  it("round-trips", async () => {
    const w = await swarld();
    const ext = { git: "https://git.example/x.git", commit: "a".repeat(40), path: "extensions/board" };
    const doc = await serviceDoc(w, { title: "Board", description: "d", docs: "https://hub.example/s/board", extension: ext });
    const p = await P.verifyService(doc, w.hub.key, { hubDomain: "hub.example", now: NOW });
    expect(p).toMatchObject({ address: SVC, extension: ext, default: "on", kind: "board" });
    await P.verifyService(await serviceDoc(w, { kind: "custom", extension: { ...ext, commit: "b".repeat(64) } }), w.hub.key, { now: NOW });
  });

  it("wrong signer or domain", async () => {
    const w = await swarld();
    await expect(P.verifyService(await serviceDoc(w, { signer: w.other }), w.hub.key, { now: NOW })).rejects.toThrow(SignatureError);
    await expect(P.verifyService(await serviceDoc(w, { signer: w.member }), w.hub.key, { now: NOW })).rejects.toThrow(SignatureError);
    await expect(P.verifyService(await serviceDoc(w, { address: "board@other.example" }), w.hub.key, { hubDomain: "hub.example", now: NOW })).rejects.toThrow(ValidationError);
    await expect(P.verifyService(await serviceDoc(w, { address: "Board" }), w.hub.key, { now: NOW })).rejects.toThrow(ValidationError);
  });

  const good = { git: "https://git.example/x.git", commit: "a".repeat(40), path: "p" };
  const badFields: any[] = [
    { kind: "forum" },
    { kind: "" },
    { roles: ["Mod"] },
    { roles: ["a b"] },
    { roles: ["x", "x"] },
    { default: "maybe" },
    { docs: "http://x.example/" },
    { extension: { ...good, git: "http://git.example/x.git" } },
    { extension: { ...good, commit: "A".repeat(40) } },
    { extension: { ...good, commit: "a".repeat(39) } },
    { extension: { ...good, commit: "main" } },
    { extension: { ...good, path: "../p" } },
    { extension: { ...good, path: "a/../p" } },
    { extension: { ...good, path: "/etc" } },
    { extension: { ...good, path: "" } },
  ];
  it.each(badFields)("refuses bad fields %j", async (kw) => {
    const w = await swarld();
    await expect(P.verifyService(await serviceDoc(w, kw), w.hub.key, { now: NOW })).rejects.toThrow(ValidationError);
  });

  it("servicesIn skips bad entries and says why", async () => {
    const w = await swarld();
    const goodDoc = await serviceDoc(w);
    const evil = await serviceDoc(w, { signer: w.other, address: "relay@hub.example", kind: "relay" });
    const foreign = await serviceDoc(w, { address: "x1y@other.example" });
    const junk = JSON.parse(JSON.stringify(goodDoc));
    junk.payload = b64.encode(b64.utf8('{"v":"l0w/0.1","type":"service"}'));
    const caps = (extras: any[]) =>
      P.validateHub({ ...JSON.parse(vec("hub").payload_json), keys: [w.hub.key.toJSON()], domain: "hub.example", extras });
    const c = await caps([
      { kind: "accelerator", id: "cloudflared" },
      { kind: "service", doc: goodDoc },
      { kind: "service", doc: evil },
      { kind: "service", doc: foreign },
      { kind: "service", doc: junk },
      { kind: "service" },
      { kind: "service", doc: goodDoc },
    ]);
    const [ok, skipped] = await P.servicesIn(c, { now: NOW });
    expect(ok.map((s) => s.address)).toEqual([SVC]);
    expect(skipped).toHaveLength(5);
    expect(skipped.every((s) => s.startsWith("extras["))).toBe(true);
    const [ok2, sk2] = await P.servicesIn(c, { hubKey: w.hub.key, now: NOW });
    expect(ok2).toEqual(ok);
    expect(sk2).toHaveLength(5);
    expect(await P.servicesIn(await caps([]), { now: NOW })).toEqual([[], []]);
    const [ok3] = await P.servicesIn(c, { hubKey: w.other.key, now: NOW });
    expect(ok3.map((s) => s.address)).toEqual(["relay@hub.example"]);
  });
});

describe("role grants", () => {
  it("verifies and compares what is given", async () => {
    const w = await swarld();
    const p = await P.verifyRoleGrant(w.grant, w.svc.key, { service: SVC, holder: ADDR, holderKid: w.member.kid, now: NOW });
    expect(p.role).toBe("moderator");
    await expect(P.verifyRoleGrant(w.grant, w.other.key, { now: NOW })).rejects.toThrow(SignatureError);
    for (const kw of [{ service: "relay@hub.example" }, { holder: "bob@hub.example" }, { holderKid: w.svc.kid }]) {
      await expect(P.verifyRoleGrant(w.grant, w.svc.key, { now: NOW, ...kw })).rejects.toThrow(SignatureError);
    }
  });

  it("exp and shape", async () => {
    const w = await swarld();
    const exp = await docs.signDocument("role-grant", P.buildRoleGrant(SVC, ADDR, w.member.kid, "poster", IAT, NOW + 10), [w.svc]);
    await P.verifyRoleGrant(exp, w.svc.key, { now: NOW });
    await expect(P.verifyRoleGrant(exp, w.svc.key, { now: NOW + 11 })).rejects.toThrow(ValidationError);
    for (const bad of [
      P.buildRoleGrant(SVC, ADDR, w.member.kid, "Not A Role", IAT),
      P.buildRoleGrant(SVC, ADDR, "k2.zzz", "poster", IAT),
      P.buildRoleGrant("board", ADDR, w.member.kid, "poster", IAT),
    ]) {
      await expect(P.verifyRoleGrant(await docs.signDocument("role-grant", bad, [w.svc]), w.svc.key, { now: NOW })).rejects.toThrow(ValidationError);
    }
    const two = await docs.signDocument("role-grant", P.buildRoleGrant(SVC, ADDR, w.member.kid, "poster", IAT), [w.svc, w.member]);
    await expect(P.verifyRoleGrant(two, w.svc.key, { now: NOW })).rejects.toThrow(SignatureError);
  });
});

describe("service envelope kinds", () => {
  it("service-hello is tokenless", async () => {
    const w = await swarld();
    const hello = { token: w.mtok, release: "0.9.0" };
    const doc = await sendSvc(w, "service-hello", hello, null);
    expect("token" in M.envelopePayload(doc)).toBe(false);
    const [p, body] = await M.openEnvelope(doc, { myAddress: SVC, mySealKeys: new Map([[w.svcSeal.kid, w.svcSeal.private]]), senderSignKey: w.member.key, now: NOW });
    expect(p.kind).toBe("service-hello");
    expect(body).toEqual(hello);
    await expect(sendSvc(w, "service-hello", hello, w.mtok)).rejects.toThrow(ValidationError);
    await expect(sendSvc(w, "service-call", { call: CALL, method: "m", args: {} }, null)).rejects.toThrow(ValidationError);
    await expect(sendSvc(w, "service-hello", { token: w.mtok }, null)).rejects.toThrow(ValidationError);
    await expect(sendSvc(w, "service-hello", { release: "1" }, null)).rejects.toThrow(ValidationError);
  });

  it("service-welcome and service-bye", async () => {
    const w = await swarld();
    P.validateBody("service-welcome", { token: w.stok, roles: [w.grant] });
    P.validateBody("service-welcome", { token: w.stok, roles: [] });
    for (const bad of [{ token: w.stok }, { token: w.stok, roles: [{ type: "x" }] }, { token: w.stok, roles: [w.stok] }, { roles: [] }]) {
      expect(() => P.validateBody("service-welcome", bad)).toThrow(ValidationError);
    }
    await sendSvc(w, "service-bye", {});
  });

  it("service-call body", async () => {
    const w = await swarld();
    const good = { call: CALL, method: "post.create", args: { a: 1 }, role: w.grant };
    await sendSvc(w, "service-call", good);
    await sendSvc(w, "service-call", { ...good, role: null });
    const { role: _r, ...noRole } = good;
    await sendSvc(w, "service-call", noRole);
    for (const bad of [
      { ...good, call: "short" },
      { ...good, call: 5 },
      { ...good, method: "" },
      { ...good, method: null },
      { ...good, args: [] },
      { ...good, args: null },
      { ...good, role: { type: "role-grant" } },
      { ...good, role: "moderator" },
    ]) {
      expect(() => P.validateBody("service-call", bad), JSON.stringify(bad)).toThrow(expect.objectContaining({ code: "bad_body" }));
    }
    expect(() => P.validateBody("service-call", { ...good, args: { x: "a".repeat(1024 * 1024) } })).toThrow(ValidationError);
    P.validateBody("service-call", { ...good, args: { x: "a".repeat(1024 * 1024 - 2000) } });
  });

  it("service-reply body", () => {
    P.validateBody("service-reply", { call: CALL, ok: true, result: { a: 1 } });
    P.validateBody("service-reply", { call: CALL, ok: false, error: { code: "not_allowed", detail: "x" } });
    P.validateBody("service-reply", { call: CALL, ok: false, error: { code: "not_allowed" } });
    for (const bad of [
      { call: CALL, ok: true },
      { call: CALL, ok: true, result: [] },
      { call: CALL, ok: "yes", result: {} },
      { call: CALL, ok: false },
      { call: CALL, ok: false, error: { detail: "x" } },
      { call: CALL, ok: false, error: "nope" },
      { ok: true, result: {} },
      { call: CALL, ok: true, result: { x: "a".repeat(1024 * 1024) } },
    ]) {
      expect(() => P.validateBody("service-reply", bad), JSON.stringify(bad)).toThrow(ValidationError);
    }
  });

  it("service-event body", async () => {
    const w = await swarld();
    P.validateBody("service-event", { event: "post.new", data: { x: 1 } });
    P.validateBody("service-event", { event: "roles", data: { grants: [w.grant] } });
    P.validateBody("service-event", { event: "roles", data: { grants: [] } });
    for (const bad of [{ event: "roles", data: {} }, { event: "roles", data: { grants: [1] } }, { event: "x", data: [] }, { data: {} }, { event: 1, data: {} }]) {
      expect(() => P.validateBody("service-event", bad), JSON.stringify(bad)).toThrow(ValidationError);
    }
  });

  it("knows the service kinds", () => {
    expect(P.KINDS).toContain("friend-request");
    expect(P.KINDS).toContain("message");
    for (const k of P.SERVICE_KINDS) expect(P.KINDS).toContain(k);
    expect(() => P.validateBody("service-nope", {})).toThrow(ProtocolError);
  });
});

describe("builders reproduce the vectors", () => {
  it("service and role-grant", async () => {
    const hub = await fromSeed(b64.fromHex(vec("keys").keys["hub-sign"].seed), "sign", IAT);
    const sv = vec("service");
    const sp = JSON.parse(sv.payload_json);
    const built = P.buildService(sp.address, sp.kind, sp.roles, { title: sp.title, description: sp.description, docs: sp.docs, extension: sp.extension, default: sp.default, iat: sp.iat });
    expect(b64.fromUtf8(docs.serializePayload(built))).toBe(sv.payload_json);
    expect(await docs.signDocument("service", built, [hub])).toEqual(sv.document);
    const rv = vec("role-grant");
    const rp = JSON.parse(rv.payload_json);
    const svcKey = await fromSeed(b64.fromHex(rv.service_key.seed), "sign", rv.service_key.key.created);
    const g = P.buildRoleGrant(rp.service, rp.holder, rp.holder_kid, rp.role, rp.iat);
    expect(await docs.signDocument("role-grant", g, [svcKey])).toEqual(rv.document);
  });
});
