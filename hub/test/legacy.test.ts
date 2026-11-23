import { describe, expect, it } from "vitest";
import { handleApi } from "../src/api";
import { loadConfig } from "../src/config";
import type { Env } from "../src/env";
import * as b64 from "../src/proto/b64";
import * as P from "../src/proto/payloads";
import { fakeD1 } from "./d1";
import { parseNow, vec } from "./util";

const env = {
  DB: fakeD1(), // the capabilities list the registered hub services
  HUB_NAME: "Example hub",
  HUB_BASE_URL: "https://hub.example",
  TUNNEL_URL: "wss://tunnel.hub.example/v0/tunnel",
  INSTANCE_HOST_PATTERN: "{name}.hub.example",
  FILES_HOST_PATTERN: "{name}.hubfiles.example",
  HUB_SIGNING_SEED: b64.encode(new Uint8Array(32).fill(7)),
} as Env;

describe("instances from before own-URL mode was removed", () => {
  it("a registration carrying mode and own_urls is accepted as a normal one (the fields are ignored)", async () => {
    const v = vec("registration");
    const legacy = {
      ...JSON.parse(v.payload_json),
      mode: "own-url",
      own_urls: { instance: "https://a.example", files: "https://b.example" },
    };
    const out = await P.validateRegistration(legacy, "hub.example", parseNow(v.now));
    expect(out.name).toBe(legacy.name);
  });

  it("the capabilities still publish membership.own_url = false", async () => {
    const r = await handleApi(new Request("https://hub.example/.well-known/l0wagent/hub"), env, loadConfig(env));
    expect(r.status).toBe(200);
    const doc = (await r.json()) as { payload: string };
    const payload = JSON.parse(new TextDecoder().decode(b64.decode(doc.payload)));
    expect(payload.membership.own_url).toBe(false);
  });
});
