import { describe, expect, it } from "vitest";
import * as b64 from "../src/b64";
import { SealError } from "../src/errors";
import * as S from "../src/seal";
import { thrown } from "./util";

const H = b64.fromHex;
const hex = b64.toHex;

describe("HPKE", () => {
  it("matches RFC 9180 A.2.1 (key derivation, key schedule, seal and open)", () => {
    // RFC 9180 A.2.1 is the X25519 / HKDF-SHA256 / ChaCha20Poly1305 base-mode vector.
    const info = H("4f6465206f6e2061204772656369616e2055726e");
    const [skE, pkE] = S.deriveKeyPair(H("909a9b35d3dc4713a5e72a4da274b55d3d3821a37e5d099e74a647db583a904b"));
    const [skR, pkR] = S.deriveKeyPair(H("1ac01f181fdf9f352797655161c58b75c656a6cc2716dcb66372da835542e1df"));
    expect(hex(pkE)).toBe("1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a");
    expect(hex(skE)).toBe("f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600");
    expect(hex(pkR)).toBe("4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a");
    expect(hex(skR)).toBe("8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb");
    const shared = S.sharedSecret(S.dh(skE, pkR), pkE, pkR);
    expect(hex(shared)).toBe("0bbe78490412b4bbea4812666f7916932b828bba79942424abb65244930d69a7");
    const [key, nonce] = S.keySchedule(shared, info);
    expect(hex(key)).toBe("ad2744de8e17f4ebba575b3f5f5a8fa1f69c2a07f6e7500bc60ca6e3e3ec1c91");
    expect(hex(nonce)).toBe("5c4d98150661b848853b547f");
    const pt = new TextEncoder().encode("Beauty is truth, truth beauty");
    const aad = new TextEncoder().encode("Count-0");
    const [enc, ct] = S.hpkeSeal(pkR, info, aad, pt, { ephPriv: skE });
    expect(hex(enc)).toBe(hex(pkE));
    expect(hex(ct)).toBe("1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938e131f0def8c8b60b4db21993c62ce81883d2dd1b51a28");
    expect(hex(S.hpkeOpen(skR, enc, info, aad, ct))).toBe(hex(pt));
  });

  it("seals and opens the document form, binding the info", () => {
    const sk = H("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20");
    const pub = S.publicKey(sk);
    const s = S.seal(pub, b64.utf8("x"), b64.utf8("i"));
    expect(s.alg).toBe(S.ALG);
    expect(b64.fromUtf8(S.openSealed(sk, s, b64.utf8("i")))).toBe("x");
    expect(S.openSealed(sk, S.seal(pub, new Uint8Array(0), b64.utf8("i")), b64.utf8("i")).length).toBe(0);
  });

  it("fails closed", async () => {
    const sk = H("0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20");
    const s = S.seal(S.publicKey(sk), b64.utf8("x"), b64.utf8("i"));
    for (const bad of [null, {}, { ...s, alg: "other" }, { ...s, enc: "AA" }, { ...s, ct: "" }, { ...s, enc: 5 }, "x", []]) {
      const e = await thrown(() => S.openSealed(sk, bad, b64.utf8("i")));
      expect(e).toBeInstanceOf(SealError);
      expect(e.code).toBe("bad_seal");
    }
    expect(await thrown(() => S.openSealed(sk, s, b64.utf8("other info")))).toBeInstanceOf(SealError);
    expect(await thrown(() => S.openSealed(new Uint8Array(32).fill(7), s, b64.utf8("i")))).toBeInstanceOf(SealError);
    expect(await thrown(() => S.seal(new Uint8Array(5), b64.utf8("x"), b64.utf8("i")))).toBeInstanceOf(SealError);
    // a low-order public key (all zero) is refused, not agreed on
    expect(await thrown(() => S.hpkeSeal(new Uint8Array(32), b64.utf8("i"), new Uint8Array(0), b64.utf8("x")))).toBeInstanceOf(SealError);
  });

  it("builds the info string", () => {
    expect(b64.fromUtf8(S.sealInfo("a@h", "k1.a", "b@h", "k1.b", "id"))).toBe("l0wagent-seal/1\na@h\nk1.a\nb@h\nk1.b\nid");
    expect(() => S.sealInfo("é@h", "k1.a", "b@h", "k1.b", "id")).toThrow(SealError);
  });
});
