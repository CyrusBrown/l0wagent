import { afterEach, describe, expect, it, vi } from "vitest";
import * as cf from "../src/cloudflare";
import { loadConfig } from "../src/config";
import type { Accel } from "../src/db";
import * as docs from "../src/proto/docs";
import { Key } from "../src/proto/keys";
import * as P from "../src/proto/payloads";
import { formatTime, parseTime } from "../src/proto/times";
import { acceleratorExtra } from "../src/accelerator";
import { parseNow, vec, vectorKey } from "./util";

const NOW = parseNow("2026-11-17T21:30:00Z");

describe("accelerator-request verification", () => {
  const v = vec("accelerator-request");
  const keyOf = () => Key.fromJSON(v.verify_keys[0]);
  const mk = async (over: Record<string, unknown> = {}, iat = NOW) => {
    const sign = await vectorKey("member-sign");
    const payload = { ...JSON.parse(v.payload_json), iat: formatTime(iat), ...over };
    return docs.signDocument("accelerator-request", payload, [sign]);
  };
  const opts = { hub: "hub.example", address: "mara@hub.example", now: NOW };

  it("accepts the vector", async () => {
    const p = await P.verifyAcceleratorRequest(v.document, await keyOf(), { ...opts, now: parseNow(v.now) });
    expect(p.action).toBe("enable");
  });
  it("answers 401-class bad_sig for signature, age, hub, accelerator and address problems", async () => {
    const k = await keyOf();
    const bad = JSON.parse(JSON.stringify(v.document));
    const raw = docs.isObject(bad) ? Buffer.from(bad.payload, "base64url") : Buffer.alloc(0);
    raw[10] ^= 1;
    bad.payload = raw.toString("base64url");
    const cases: [string, unknown][] = [
      ["tampered", bad],
      ["too old", await mk({}, NOW - 121)],
      ["future", await mk({}, NOW + 301)],
      ["other hub", await mk({ hub: "other.example" })],
      ["other accelerator", await mk({ accelerator: "other" })],
      ["other address", await mk({ address: "ana@hub.example" })],
    ];
    for (const [why, d] of cases) {
      await expect(P.verifyAcceleratorRequest(d, k, opts), why).rejects.toMatchObject({ code: "bad_sig" });
    }
  });
  it("accepts an iat 120 s old and one 300 s ahead", async () => {
    const k = await keyOf();
    await P.verifyAcceleratorRequest(await mk({}, NOW - 120), k, opts);
    await P.verifyAcceleratorRequest(await mk({}, NOW + 300), k, opts);
  });
  it("answers bad_request for an unknown action and bad services", async () => {
    const k = await keyOf();
    const bads = [
      { action: "restart" },
      { service: "http://localhost:8790" },
      { service: "https://127.0.0.1:8790" },
      { service: "http://127.0.0.1:0" },
      { service: "http://127.0.0.1:65536" },
      { service: "http://127.0.0.1:8790/" },
      { service: "http://127.0.0.1" },
      { service: undefined },
    ];
    for (const b of bads) await expect(P.verifyAcceleratorRequest(await mk(b), k, opts), JSON.stringify(b)).rejects.toMatchObject({ code: "bad_request" });
    await P.verifyAcceleratorRequest(await mk({ service: "http://127.0.0.1:65535" }), k, opts);
    await P.verifyAcceleratorRequest(await mk({ action: "activate", service: undefined }), k, opts);
  });
});

describe("capabilities extra", () => {
  const base = {
    HUB_NAME: "T",
    HUB_BASE_URL: "https://hub.example",
    TUNNEL_URL: "wss://hub.example/v0/tunnel",
    INSTANCE_HOST_PATTERN: "{name}.hub.example",
    FILES_HOST_PATTERN: "{name}.files.example",
  };
  const cfg = (extra: Record<string, string>) => loadConfig({ ...base, ...extra } as never);
  const all = { CF_ACCELERATOR: "true", CF_ACCOUNT_ID: "a", CF_ZONE_ID: "z", CF_API_TOKEN: "t" };
  it("is advertised only when enabled and fully configured", () => {
    expect(acceleratorExtra(cfg({}))).toBeNull();
    expect(acceleratorExtra(cfg({ ...all, CF_ACCELERATOR: "false" }))).toBeNull();
    expect(acceleratorExtra(cfg({ ...all, CF_API_TOKEN: "" }))).toBeNull();
    expect(acceleratorExtra(cfg({ ...all, CF_ZONE_ID: "" }))).toBeNull();
    const url = "https://hub.example/v0/accelerators/cloudflared";
    expect(acceleratorExtra(cfg(all))).toEqual({ kind: "accelerator", id: "cloudflared", doc: url, api: url });
    expect(cfg(all).accelerator?.filesZoneId).toBe("z");
    expect(cfg({ ...all, CF_FILES_ZONE_ID: "fz", CF_ACCELERATOR_DOC: "https://d" }).accelerator).toMatchObject({ filesZoneId: "fz", doc: "https://d" });
  });
});

describe("Cloudflare call sequence", () => {
  const acfg = { accountId: "acct", zoneId: "z1", filesZoneId: "z2", apiBase: "https://cf.test/v4", token: "tok", doc: "", api: "" };
  const names = { member: "mara", instance: "mara.hub.example", files: "mara.files.example" };
  const empty = (): Accel => ({ name: "mara", lastIat: 0, tunnelId: null, service: null, dnsInstance: null, dnsFiles: null, routeInstance: null, routeFiles: null, active: false });
  const calls: string[] = [];
  const bodies: Record<string, any>[] = [];
  let failOn: string | null = null;
  let dnsExists = false;
  let status = "healthy";
  let n = 0;

  function stub() {
    calls.length = 0;
    bodies.length = 0;
    n = 0;
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      const path = url.replace("https://cf.test/v4", "");
      const key = `${init.method} ${path}`;
      calls.push(key);
      bodies.push(init.body ? JSON.parse(init.body as string) : {});
      expect((init.headers as Record<string, string>).authorization).toBe("Bearer tok");
      const send = (result: unknown, success = true, code = 0, msg = "") =>
        new Response(JSON.stringify({ success, errors: success ? [] : [{ code, message: msg }], result }), { status: success ? 200 : 400 });
      if (failOn && key.startsWith(failOn)) {
        failOn = null;
        return send(null, false, 1000, "boom");
      }
      if (key === "POST /accounts/acct/cfd_tunnel") return send({ id: "T1" });
      if (key === "GET /accounts/acct/cfd_tunnel/T1/token") return send("TOKEN");
      if (key === "GET /accounts/acct/cfd_tunnel/T1") return send({ status });
      if (key === "POST /zones/z1/dns_records" && dnsExists) return send(null, false, 81057, "Record already exists.");
      if (key.startsWith("GET /zones/z1/dns_records?name=")) return send([{ id: "OLD" }]);
      if (key.startsWith("POST /zones/")) return send({ id: `id${++n}` });
      return send({});
    });
  }
  afterEach(() => {
    vi.unstubAllGlobals();
    failOn = null;
    dnsExists = false;
    status = "healthy";
  });

  const saver = (a: Accel) => async (p: Record<string, unknown>) => void Object.assign(a, p);

  it("enable: tunnel, ingress, two CNAMEs, token", async () => {
    stub();
    const a = empty();
    const r = await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    expect(calls).toEqual([
      "POST /accounts/acct/cfd_tunnel",
      "PUT /accounts/acct/cfd_tunnel/T1/configurations",
      "POST /zones/z1/dns_records",
      "POST /zones/z2/dns_records",
      "GET /accounts/acct/cfd_tunnel/T1/token",
    ]);
    expect(bodies[0]).toMatchObject({ config_src: "cloudflare" });
    expect(bodies[0].name).toMatch(/^l0w-mara-[0-9a-f]{8}$/);
    expect(bodies[1].config.ingress).toEqual([
      { hostname: "mara.hub.example", service: "http://127.0.0.1:8790" },
      { hostname: "mara.files.example", service: "http://127.0.0.1:8790" },
      { service: "http_status:404" },
    ]);
    expect(bodies[2]).toEqual({ type: "CNAME", name: "mara.hub.example", content: "T1.cfargotunnel.com", proxied: true });
    expect(r.token).toBe("TOKEN");
    expect(a).toMatchObject({ tunnelId: "T1", service: "http://127.0.0.1:8790", dnsInstance: "id1", dnsFiles: "id2" });
  });

  it("enable again reuses the tunnel and the records", async () => {
    stub();
    const a = empty();
    await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    calls.length = 0;
    await cf.enable(acfg, names, a, "http://127.0.0.1:9000", saver(a));
    expect(calls).toEqual(["PUT /accounts/acct/cfd_tunnel/T1/configurations", "GET /accounts/acct/cfd_tunnel/T1/token"]);
    expect(a.service).toBe("http://127.0.0.1:9000");
  });

  it("a failure keeps the progress and a retry continues", async () => {
    stub();
    const a = empty();
    failOn = "POST /zones/z2/dns_records";
    await expect(cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a))).rejects.toBeInstanceOf(cf.CfError);
    expect(a).toMatchObject({ tunnelId: "T1", dnsInstance: "id1", dnsFiles: null });
    calls.length = 0;
    await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    expect(calls).not.toContain("POST /accounts/acct/cfd_tunnel");
    expect(calls).not.toContain("POST /zones/z1/dns_records");
    expect(a.dnsFiles).toBe("id2" /* the counter restarts only with stub() */);
  });

  it("an existing DNS record is found and updated", async () => {
    stub();
    dnsExists = true;
    const a = empty();
    await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    expect(calls).toContain("GET /zones/z1/dns_records?name=mara.hub.example");
    expect(calls).toContain("PUT /zones/z1/dns_records/OLD");
    expect(a.dnsInstance).toBe("OLD");
  });

  it("activate needs a live tunnel, then makes two script-less routes", async () => {
    stub();
    const a = empty();
    await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    calls.length = 0;
    status = "inactive";
    await expect(cf.activate(acfg, names, a, saver(a))).rejects.toBeInstanceOf(cf.NotConnected);
    expect(calls).toEqual(["GET /accounts/acct/cfd_tunnel/T1"]);
    status = "degraded";
    calls.length = 0;
    bodies.length = 0;
    const s = await cf.activate(acfg, names, a, saver(a));
    expect(calls.slice(1)).toEqual(["POST /zones/z1/workers/routes", "POST /zones/z2/workers/routes"]);
    expect(bodies.slice(1)).toEqual([{ pattern: "mara.hub.example/*" }, { pattern: "mara.files.example/*" }]);
    expect(s.active).toBe(true);
    expect(a.active).toBe(true);
  });

  it("deactivate deletes the routes; disable deletes everything and clears the state", async () => {
    stub();
    const a = empty();
    await cf.enable(acfg, names, a, "http://127.0.0.1:8790", saver(a));
    await cf.activate(acfg, names, a, saver(a));
    const [ri, rf] = [a.routeInstance, a.routeFiles];
    calls.length = 0;
    await cf.deactivate(acfg, a, saver(a));
    expect(calls).toEqual([`DELETE /zones/z1/workers/routes/${ri}`, `DELETE /zones/z2/workers/routes/${rf}`]);
    expect(a).toMatchObject({ active: false, routeInstance: null, routeFiles: null, tunnelId: "T1" });
    await cf.activate(acfg, names, a, saver(a));
    calls.length = 0;
    await cf.disable(acfg, a, saver(a));
    const ids = [a.routeInstance, a.routeFiles, a.dnsInstance, a.dnsFiles];
    expect(ids.every(Boolean)).toBe(false); // cleared already; the calls below were made with the old ids
    expect(calls).toHaveLength(6);
    expect(calls.slice(0, 4).map((c) => c.split("/").slice(0, 4).join("/"))).toEqual([
      "DELETE /zones/z1/workers",
      "DELETE /zones/z2/workers",
      "DELETE /zones/z1/dns_records",
      "DELETE /zones/z2/dns_records",
    ]);
    expect(calls.slice(4)).toEqual(["DELETE /accounts/acct/cfd_tunnel/T1/connections", "DELETE /accounts/acct/cfd_tunnel/T1"]);
    expect(a).toMatchObject({ tunnelId: null, service: null, dnsInstance: null, dnsFiles: null, active: false });
  });

  it("the iat helper parses what the hub stores", () => {
    expect(parseTime(formatTime(NOW))).toBe(NOW);
  });
});
