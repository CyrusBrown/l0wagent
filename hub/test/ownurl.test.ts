import { afterEach, describe, expect, it, vi } from "vitest";
import { checkOwnUrls, HubError } from "../src/api";
import { type Config, loadConfig } from "../src/config";
import type { Env } from "../src/env";
import * as docs from "../src/proto/docs";
import * as P from "../src/proto/payloads";
import { computeKid, generate } from "../src/proto/keys";
import * as b64 from "../src/proto/b64";

const cfg = (own = "true"): Config =>
  loadConfig({
    HUB_NAME: "h",
    HUB_BASE_URL: "https://hub.example",
    TUNNEL_URL: "wss://hub.example/v0/tunnel",
    INSTANCE_HOST_PATTERN: "{name}.hub.example",
    FILES_HOST_PATTERN: "{name}.hubfiles.example",
    OWN_URL: own,
  } as Env);

async function member() {
  const [sign, recover] = await Promise.all([generate("sign"), generate("recover")]);
  return { sign, recover };
}

async function selfDoc(address: string, signer: Awaited<ReturnType<typeof generate>>, others: Awaited<ReturnType<typeof generate>>[]) {
  // the self validator needs one key of each use; seal keys are only parsed, so a fixed X25519 public key will do
  const seal = {
    kid: "",
    alg: "X25519",
    use: "seal",
    pub: "TvI1yw2S0w404arbvNlWSilV5iTAPKMvhBWXmUJrFiE",
    created: "2026-11-17T21:30:00Z",
  };
  seal.kid = await computeKid("X25519", b64.decode(seal.pub));
  const p = {
    v: "l0w/0.1",
    type: "self",
    iat: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    address,
    seq: 0,
    keys: [signer.key.toJSON(), seal, others[0].key.toJSON()],
    history: [],
    hostnames: { instance: "box.example", files: "files.example" },
    protocols: ["l0w/0.1"],
  };
  return docs.signDocument("self", p, [signer]);
}

afterEach(() => vi.unstubAllGlobals());

describe("own-url registration checks", () => {
  const urls = { instance: "https://box.mara.example", files: "https://files.mara.example" };

  it("accepts a reachable instance that serves the member's self document", async () => {
    const m = await member();
    const doc = await selfDoc("mara@hub.example", m.sign, [m.recover]);
    const seen: string[] = [];
    vi.stubGlobal("fetch", async (u: string) => {
      seen.push(u);
      return u.includes("/.well-known/l0wagent/self") ? new Response(JSON.stringify(doc)) : new Response("files");
    });
    await checkOwnUrls(cfg(), { name: "mara", own_urls: urls }, m.sign.key);
    expect(seen).toEqual(["https://box.mara.example/.well-known/l0wagent/self", "https://files.mara.example"]);
  });

  it("refuses the same origin, an unreachable URL, another member's document and a non-200", async () => {
    const m = await member();
    const other = await member();
    const doc = await selfDoc("mara@hub.example", other.sign, [other.recover]);
    const reg = (own_urls: object) => checkOwnUrls(cfg(), { name: "mara", own_urls }, m.sign.key);
    await expect(reg({ instance: "https://a.example", files: "https://a.example/x" })).rejects.toMatchObject({ status: 422, code: "own_url_unreachable" });
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("network down");
    });
    await expect(reg(urls)).rejects.toMatchObject({ status: 422, code: "own_url_unreachable" });
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify(doc)));
    await expect(reg(urls)).rejects.toMatchObject({ status: 422, code: "own_url_unreachable" });
    vi.stubGlobal("fetch", async () => new Response("nope", { status: 404 }));
    await expect(reg(urls)).rejects.toBeInstanceOf(HubError);
  });

  it("is off when OWN_URL is false", async () => {
    const m = await member();
    await expect(checkOwnUrls(cfg("false"), { name: "mara", own_urls: urls }, m.sign.key)).rejects.toMatchObject({ status: 400 });
  });
});
