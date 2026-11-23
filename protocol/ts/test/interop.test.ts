// TS <-> Python round trips: the TypeScript library seals and signs, the Python reference library opens and
// verifies, and the other way round. Skipped when `uv` is not installed (or protocol/py cannot run).
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import * as b64 from "../src/b64";
import * as docs from "../src/docs";
import { fromSeed } from "../src/keys";
import * as M from "../src/messages";
import * as P from "../src/payloads";
import { ADDR, IAT, NOW } from "./util";

const PY_DIR = join(import.meta.dirname, "..", "..", "py");

function py(code: string, input: unknown): any {
  const r = spawnSync("uv", ["run", "python", "-c", code], { cwd: PY_DIR, input: JSON.stringify(input), encoding: "utf8", timeout: 60_000 });
  if (r.error || r.status !== 0) throw new Error(`python failed: ${r.error ?? r.stderr}`);
  return JSON.parse(r.stdout);
}

const have = (() => {
  const r = spawnSync("uv", ["run", "python", "-c", "import l0wproto"], { cwd: PY_DIR, encoding: "utf8", timeout: 60_000 });
  return !r.error && r.status === 0;
})();

const BOB = "bob@other.example";
const seed = (n: number) => new Uint8Array(32).fill(n);
const hex = b64.toHex;

// Python opens what TS made: reads {doc, address, seal_seed, sender_key, now} and prints {payload, body}.
const PY_OPEN = `
import json, sys
from l0wproto import keys, messages as M, docs, b64
i = json.load(sys.stdin)
seed = bytes.fromhex(i["seal_seed"])
sk = keys.from_seed(seed, "seal", i["created"])
sender = keys.Key.from_dict(i["sender_key"])
p, body = M.open_envelope(i["doc"], my_address=i["address"], my_seal_keys={sk.kid: seed}, sender_sign_key=sender, now=i["now"])
tok = M.verify_delivery_token(p["token"], keys.Key.from_dict(i["issuer_key"]), now=i["now"]) if "token" in p else None
json.dump({"payload": p, "body": body, "token": tok, "token_id": M.token_id(i["doc"])}, sys.stdout)
`;

// Python makes an envelope: reads the keys and the body, prints {doc, token}.
const PY_MAKE = `
import json, sys
from l0wproto import keys, messages as M, docs, payloads as P
i = json.load(sys.stdin)
C = i["created"]
mara = keys.from_seed(bytes.fromhex(i["mara_seed"]), "sign", C)
bob = keys.from_seed(bytes.fromhex(i["bob_seed"]), "sign", C)
seal = keys.from_seed(bytes.fromhex(i["seal_seed"]), "seal", C)
tok = docs.sign_document("delivery-token", P.build_delivery_token(i["bob"], bob.kid, i["mara"], mara.kid, iat=C), [bob])
doc = M.make_envelope(mara, mara.kid, i["mara"], i["bob"], seal.key, "message", i["body"], 7, tok, iat=C)
fr = M.make_envelope(mara, mara.kid, i["mara"], i["bob"], seal.key, "friend-request",
                     {"code": None, "note": "hi", "by": "owner", "token": tok}, 1, None, iat=C)
json.dump({"doc": doc, "friend_request": fr, "token": tok, "fingerprint": M.fingerprint(mara.key.pub),
           "friend_code": M.make_friend_code(i["mara"], M.fingerprint(mara.key.pub), bytes(range(16)))[0]}, sys.stdout)
`;

describe.skipIf(!have)("TypeScript <-> Python interop", () => {
  it("TS seals and signs, Python opens and verifies", async () => {
    const mara = await fromSeed(seed(1), "sign", IAT);
    const bob = await fromSeed(seed(2), "sign", IAT);
    const bobSeal = await fromSeed(seed(3), "seal", IAT);
    const tok = await M.issueDeliveryToken(bob, BOB, ADDR, mara.kid, { iat: IAT });
    const body = { thread: "t-1", reply_to: null, text: "Hello from TypeScript é \u{1F331}", files: [P.buildFile("a.txt", "text/plain", b64.utf8("abc"))] };
    const doc = await M.makeEnvelope(mara, mara.kid, ADDR, BOB, bobSeal.key, "message", body, 3, tok, { iat: IAT });
    const out = py(PY_OPEN, {
      doc,
      address: BOB,
      seal_seed: hex(seed(3)),
      created: IAT,
      sender_key: mara.key.toJSON(),
      issuer_key: bob.key.toJSON(),
      now: NOW,
    });
    expect(out.body).toEqual(body);
    expect(out.payload.seq).toBe(3);
    expect(out.token.holder).toBe(ADDR);
    expect(out.token_id).toBe(M.tokenId(tok));
    // a tokenless friend request, too
    const fr = await M.makeEnvelope(mara, mara.kid, ADDR, BOB, bobSeal.key, "friend-request", { code: null, note: "hi", by: "owner", token: tok }, 1, null, { iat: IAT });
    const out2 = py(PY_OPEN, { doc: fr, address: BOB, seal_seed: hex(seed(3)), created: IAT, sender_key: mara.key.toJSON(), issuer_key: bob.key.toJSON(), now: NOW });
    expect(out2.payload.kind).toBe("friend-request");
    expect(out2.token).toBeNull();
  });

  it("Python seals and signs, TS opens and verifies", async () => {
    const mara = await fromSeed(seed(1), "sign", IAT);
    const bob = await fromSeed(seed(2), "sign", IAT);
    const bobSeal = await fromSeed(seed(3), "seal", IAT);
    const body = { thread: "t-2", reply_to: "x", text: "Hello from Python é \u{1F331}", files: [] };
    const out = py(PY_MAKE, { mara_seed: hex(seed(1)), bob_seed: hex(seed(2)), seal_seed: hex(seed(3)), created: IAT, mara: ADDR, bob: BOB, body });
    const opts = { myAddress: BOB, mySealKeys: new Map([[bobSeal.kid, bobSeal.private]]), senderSignKey: mara.key, now: NOW };
    const [p, got] = await M.openEnvelope(out.doc, opts);
    expect(got).toEqual(body);
    expect(p.seq).toBe(7);
    expect(await M.verifyDeliveryToken(p.token, bob.key, mara.kid, NOW)).toMatchObject({ holder: ADDR });
    const [p2, got2] = await M.openEnvelope(out.friend_request, opts);
    expect(p2.kind).toBe("friend-request");
    expect(got2.by).toBe("owner");
    // the same primitives agree
    expect(M.fingerprint(mara.key.pub)).toBe(out.fingerprint);
    expect(M.parseFriendCode(out.friend_code)).toEqual([ADDR, out.fingerprint, Uint8Array.from({ length: 16 }, (_, i) => i)]);
    expect(M.makeFriendCode(ADDR, out.fingerprint, Uint8Array.from({ length: 16 }, (_, i) => i))[0]).toBe(out.friend_code);
    // and what Python signed re-serializes to the same bytes TS would produce
    expect(docs.serializePayload(JSON.parse(b64.fromUtf8(b64.decode(out.doc.payload))))).toEqual(b64.decode(out.doc.payload));
  });
});
