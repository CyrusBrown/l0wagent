import { describe, expect, it } from "vitest";
import { ConfigError, hostnameLayout, hostnamesFor, hubKey, loadConfig, matchHost, releaseLess } from "../src/config";
import type { Env } from "../src/env";
import * as b64 from "../src/proto/b64";

const base = (over: Partial<Env> = {}): Env =>
  ({
    HUB_NAME: "Example hub",
    HUB_BASE_URL: "https://hub.example",
    TUNNEL_URL: "wss://tunnel.hub.example/v0/tunnel",
    INSTANCE_HOST_PATTERN: "{name}.hub.example",
    FILES_HOST_PATTERN: "{name}.hubfiles.example",
    HUB_SIGNING_SEED: b64.encode(new Uint8Array(32).fill(7)),
    ...over,
  }) as Env;

describe("loadConfig", () => {
  it("derives the domain from the base URL host, with its port", () => {
    expect(loadConfig(base()).domain).toBe("hub.example");
    expect(loadConfig(base({ HUB_BASE_URL: "http://hub.localhost:8787" })).domain).toBe("hub.localhost:8787");
  });
  it("lets HUB_DOMAIN override it", () => {
    expect(loadConfig(base({ HUB_DOMAIN: "Example.COM" })).domain).toBe("example.com");
  });
  it("defaults limits and merges LIMITS over them", () => {
    expect(loadConfig(base()).limits).toEqual({ tunnel_streams: 64, request_body_bytes: 104857600, auth_rate_per_minute: 20 });
    expect(loadConfig(base({ LIMITS: '{"tunnel_streams": 8}' })).limits.tunnel_streams).toBe(8);
  });
  it("refuses a broken configuration", () => {
    expect(() => loadConfig(base({ HUB_BASE_URL: "hub.example" }))).toThrow(ConfigError);
    expect(() => loadConfig(base({ HUB_BASE_URL: "https://hub.example/path" }))).toThrow(ConfigError);
    expect(() => loadConfig(base({ INSTANCE_HOST_PATTERN: "mara.hub.example" }))).toThrow(ConfigError);
    expect(() => loadConfig(base({ LIMITS: "{" }))).toThrow(ConfigError);
    expect(() => loadConfig(base({ LIMITS: '{"tunnel_streams": -1}' }))).toThrow(ConfigError);
    expect(() => loadConfig(base({ HUB_NAME: "" }))).toThrow(ConfigError);
  });
  it("reserves the hub's own name and RESERVED_NAMES", () => {
    expect(loadConfig(base({ RESERVED_NAMES: "Foo, bar" })).reservedExtra).toEqual(["foo", "bar", "hub"]);
  });
  it("POLICIES", () => {
    expect(loadConfig(base({ POLICIES: '{"terms":"https://x/t"}' })).policies).toEqual({ terms: "https://x/t" });
  });
});

describe("hostnames", () => {
  it("fills the patterns and matches them back", () => {
    const cfg = loadConfig(base());
    expect(hostnamesFor(cfg, "mara")).toEqual({ instance: "mara.hub.example", files: "mara.hubfiles.example" });
    expect(matchHost(cfg, "mara.hub.example")).toEqual({ name: "mara", kind: "instance" });
    expect(matchHost(cfg, "MARA.hubfiles.example")).toEqual({ name: "mara", kind: "files" });
    expect(matchHost(cfg, "hub.example")).toBeNull();
    expect(matchHost(cfg, "a.b.hub.example")).toBeNull();
    expect(matchHost(cfg, "evil.example")).toBeNull();
  });
  it("handles flat patterns and ports", () => {
    const cfg = loadConfig(base({ FILES_HOST_PATTERN: "files--{name}.hub.example", HUB_BASE_URL: "http://hub.example:8787" }));
    expect(matchHost(cfg, "files--mara.hub.example")).toEqual({ name: "mara", kind: "files" });
    expect(hostnameLayout(cfg)).toBe("flat");
    const ported = loadConfig(base({ INSTANCE_HOST_PATTERN: "{name}.hub.localhost:8787", FILES_HOST_PATTERN: "files--{name}.hubfiles.localhost:8787" }));
    expect(matchHost(ported, "mara.hub.localhost:8787")).toEqual({ name: "mara", kind: "instance" });
    expect(matchHost(ported, "mara.hub.localhost")).toBeNull();
  });
  it("describes the layout", () => {
    expect(hostnameLayout(loadConfig(base()))).toBe("two-domains");
    expect(hostnameLayout(loadConfig(base({ FILES_HOST_PATTERN: "files.{name}.hub.example" })))).toBe("nested");
  });
});

describe("the hub key", () => {
  it("is derived from the seed and is stable", async () => {
    const env = base();
    const a = await hubKey(env);
    const b = await hubKey(base());
    expect(a.kid).toBe(b.kid);
    expect(a.kid.startsWith("k1.")).toBe(true);
  });
  it("refuses a bad seed", async () => {
    await expect(hubKey(base({ HUB_SIGNING_SEED: "short" }))).rejects.toThrow(ConfigError);
  });
});

describe("releaseLess", () => {
  it("compares like the fake hub's release tuples", () => {
    expect(releaseLess("0.0.1", "0.8.0")).toBe(true);
    expect(releaseLess("0.8.0", "0.8.0")).toBe(false);
    expect(releaseLess("0.9.0", "0.8.0")).toBe(false);
    expect(releaseLess("0.10.0", "0.9.0")).toBe(false);
    expect(releaseLess("1.0.0-rc1", "0.9.0")).toBe(false);
    expect(releaseLess("0.8", "0.8.0")).toBe(true); // a shorter tuple sorts first
    expect(releaseLess("garbage", "0.8.0")).toBe(true);
  });
});
