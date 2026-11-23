import { writeFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("cloudflare:workers", () => ({ DurableObject: class {} }));
import { handleApi } from "../src/api";
import { hubKey as hubKeyPair, loadConfig } from "../src/config";
import * as db from "../src/db";
import * as b64 from "../src/proto/b64";
import * as docs from "../src/proto/docs";
import { type KeyPair, generate } from "../src/proto/keys";
import * as P from "../src/proto/payloads";
import { fakeD1 } from "./d1";
import { vec } from "./util";

const SEED = b64.encode(new Uint8Array(32).fill(7));
const TOKEN = "admin-secret";
const COMMIT = "0123456789abcdef0123456789abcdef01234567";

let env: Record<string, unknown>;
const cfg = () => loadConfig(env as never);
const call = (method: string, path: string, body?: unknown, token: string | null = TOKEN) =>
  handleApi(
    new Request(`https://hub.example${path}`, {
      method,
      headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    env as never,
    cfg(),
  );

beforeEach(() => {
  env = {
    DB: fakeD1(),
    HUB_NAME: "Test hub",
    HUB_BASE_URL: "https://hub.example",
    TUNNEL_URL: "wss://hub.example/v0/tunnel",
    INSTANCE_HOST_PATTERN: "{name}.hub.example",
    FILES_HOST_PATTERN: "files-{name}.hubfiles.example",
    HUB_SIGNING_SEED: SEED,
    ADMIN_TOKEN: TOKEN,
  };
});

async function selfFor(name: string, over: Record<string, unknown> = {}, seq = 0): Promise<{ self: docs.Doc; sign: KeyPair }> {
  const sign = await generate("sign");
  const keys = [sign.key.toJSON(), vec("keys").keys["member-seal"].key, (await generate("recover")).key.toJSON()];
  const payload = {
    v: "l0w/0.1",
    type: "self",
    iat: "2026-11-22T12:00:00Z",
    address: `${name}@hub.example`,
    seq,
    keys,
    history: [],
    hostnames: { instance: `${name}.hub.example`, files: `files-${name}.hubfiles.example` },
    protocols: ["l0w/0.1"],
    ...over,
  };
  return { self: await docs.signDocument("self", payload, [sign]), sign };
}

const body = async (name: string, over: Record<string, unknown> = {}) => ({
  kind: "board",
  title: "Board",
  description: "Topics and posts.",
  docs: "https://hub.example/services/board",
  roles: ["moderator", "poster"],
  extension: { git: "https://git.example/hub-services.git", commit: COMMIT, path: "extensions/board" },
  default: "on",
  self: (await selfFor(name)).self,
  ...over,
});

async function caps(): Promise<docs.Obj> {
  const r = await handleApi(new Request("https://hub.example/.well-known/l0wagent/hub"), env as never, cfg());
  expect(r.status).toBe(200);
  return docs.parsePayload(docs.shape(await r.json()));
}

describe("admin services", () => {
  it("registers a service: capabilities list a hub-signed service document", async () => {
    const r = await call("PUT", "/v0/admin/services/board", await body("board"));
    expect(r.status).toBe(201);
    expect(await r.json()).toMatchObject({ name: "board", address: "board@hub.example", default: "on", roles: ["moderator", "poster"] });

    const c = await caps();
    const hubKey = (await hubKeyPair(env as never)).key;
    const extras = c.extras as docs.Obj[];
    const svc = extras.filter((e) => e.kind === "service");
    expect(svc).toHaveLength(1);
    const p = await docs.verifyDocument(svc[0].doc, [hubKey], { expectedType: "service", required: [hubKey.kid] });
    P.validateService(p, "hub.example");
    expect(p).toMatchObject({ address: "board@hub.example", kind: "board", title: "Board", default: "on", roles: ["moderator", "poster"] });
    expect(p.extension.commit).toBe(COMMIT);
    if (process.env.CAPS_OUT) {
      const r = await handleApi(new Request("https://hub.example/.well-known/l0wagent/hub"), env as never, cfg());
      writeFileSync(process.env.CAPS_OUT, JSON.stringify(await r.json()));
    }
  });

  it("serves the directory entry countersigned by the hub, like a member's", async () => {
    const b = await body("board");
    await call("PUT", "/v0/admin/services/board", b);
    const r = await handleApi(new Request("https://hub.example/.well-known/l0wagent/members/board"), env as never, cfg());
    expect(r.status).toBe(200);
    const entry = (await r.json()) as docs.Doc;
    const hubKey = (await hubKeyPair(env as never)).key;
    const p = await P.verifyDirectoryEntry(entry, hubKey, { hubDomain: "hub.example", name: "board" });
    expect(p.address).toBe("board@hub.example");
  });

  it("lists, replaces (same seq or higher) and deletes", async () => {
    await call("PUT", "/v0/admin/services/board", await body("board"));
    const second = await call("PUT", "/v0/admin/services/board", await body("board", { title: "Board 2", default: "off" }));
    expect(second.status).toBe(200);
    const list = (await (await call("GET", "/v0/admin/services")).json()) as { services: docs.Obj[] };
    expect(list.services).toHaveLength(1);
    expect(list.services[0]).toMatchObject({ title: "Board 2", default: "off" });

    expect((await call("DELETE", "/v0/admin/services/board")).status).toBe(200);
    expect((await call("DELETE", "/v0/admin/services/board")).status).toBe(404);
    expect(((await caps()).extras as docs.Obj[]).filter((e) => e.kind === "service")).toHaveLength(0);
    const r = await handleApi(new Request("https://hub.example/.well-known/l0wagent/members/board"), env as never, cfg());
    expect(r.status).toBe(404);
  });

  it("refuses a lower seq", async () => {
    await call("PUT", "/v0/admin/services/board", { ...(await body("board")), self: (await selfFor("board", {}, 5)).self });
    const r = await call("PUT", "/v0/admin/services/board", { ...(await body("board")), self: (await selfFor("board", {}, 4)).self });
    expect(r.status).toBe(409);
  });

  it("refuses bad documents and inputs", async () => {
    const cases: [string, Record<string, unknown>, string][] = [
      ["kind", { kind: "forum" }, "bad_request"],
      ["no title", { title: undefined }, "bad_request"],
      ["role", { roles: ["Bad Role"] }, "bad_request"],
      ["dup role", { roles: ["a", "a"] }, "bad_request"],
      ["roles not a list", { roles: "poster" }, "bad_request"],
      ["docs http", { docs: "http://hub.example/x" }, "bad_request"],
      ["git http", { extension: { git: "http://git.example/x.git", commit: COMMIT, path: "e" } }, "bad_request"],
      ["short commit", { extension: { git: "https://git.example/x.git", commit: "abc123", path: "e" } }, "bad_request"],
      ["abs path", { extension: { git: "https://git.example/x.git", commit: COMMIT, path: "/e" } }, "bad_request"],
      ["dotdot", { extension: { git: "https://git.example/x.git", commit: COMMIT, path: "a/../b" } }, "bad_request"],
      ["default", { default: "maybe" }, "bad_request"],
      ["no self", { self: undefined }, "bad_self"],
    ];
    for (const [why, over, code] of cases) {
      const r = await call("PUT", "/v0/admin/services/board", { ...(await body("board")), ...over });
      expect(r.status, why).toBe(400);
      expect(((await r.json()) as docs.Obj).error, why).toBe(code);
    }
    // self for another address, wrong hostnames, tampered signature
    for (const [why, self] of [
      ["address", (await selfFor("spaces")).self],
      ["hostnames", (await selfFor("board", { hostnames: { instance: "x.hub.example", files: "y.example" } })).self],
    ] as const) {
      const r = await call("PUT", "/v0/admin/services/board", { ...(await body("board")), self });
      expect(r.status, why).toBe(400);
      expect(((await r.json()) as docs.Obj).error, why).toBe("bad_self");
    }
    const t = (await selfFor("board")).self;
    const raw = Buffer.from(t.payload, "base64url");
    raw[12] ^= 1;
    const r = await call("PUT", "/v0/admin/services/board", { ...(await body("board")), self: { ...t, payload: raw.toString("base64url") } });
    expect(r.status).toBe(400);
    expect((await (await call("GET", "/v0/admin/services")).json()) as unknown).toEqual({ services: [] });
  });

  it("refuses a name a member has, and keeps members from taking a service's name", async () => {
    const sign = await generate("sign");
    const keys = { sign: sign.key.toJSON(), seal: vec("keys").keys["member-seal"].key, recover: (await generate("recover")).key.toJSON() } as db.KeySet;
    await db.insertMember(env.DB as D1Database, {
      name: "mara",
      address: "mara@hub.example",
      keys,
      hostnames: { instance: "mara.hub.example", files: "files-mara.hubfiles.example" },
      release: "0.9.0",
      now: 1,
    });
    const r = await call("PUT", "/v0/admin/services/mara", await body("mara"));
    expect(r.status).toBe(409);
    // a reserved name is fine for a service
    expect((await call("PUT", "/v0/admin/services/spaces", await body("spaces", { kind: "spaces" }))).status).toBe(201);
    expect((await call("PUT", "/v0/admin/services/admin", await body("admin", { kind: "admin" }))).status).toBe(201);
  });

  it("answers 404 without an ADMIN_TOKEN, 401 with a wrong one", async () => {
    delete env.ADMIN_TOKEN;
    for (const [m, p] of [["GET", "/v0/admin/services"], ["PUT", "/v0/admin/services/board"], ["DELETE", "/v0/admin/services/board"]]) {
      expect((await call(m, p, m === "PUT" ? await body("board") : undefined)).status, m).toBe(404);
    }
    env.ADMIN_TOKEN = TOKEN;
    expect((await call("GET", "/v0/admin/services", undefined, "wrong")).status).toBe(401);
    expect((await call("GET", "/v0/admin/services", undefined, null)).status).toBe(401);
  });

  it("builds the vector's service document", () => {
    const v = vec("service");
    expect(P.buildService({ ...JSON.parse(v.payload_json), iat: "2026-11-17T21:30:00Z" })).toEqual(JSON.parse(v.payload_json));
  });

  it("a service's hostname is answered 404 service_not_here by the hub, not as a member or the API", async () => {
    const worker = (await import("../src/index")).default;
    const get = (host: string) => worker.fetch(new Request(`https://${host}/.well-known/l0wagent/hub`), env as never);
    expect((await get("board.hub.example")).status).toBe(200); // not registered yet: just the hub's API
    await call("PUT", "/v0/admin/services/board", await body("board"));
    for (const host of ["board.hub.example", "files-board.hubfiles.example"]) {
      const r = await get(host);
      expect(r.status, host).toBe(404);
      expect(((await r.json()) as docs.Obj).error).toBe("service_not_here");
    }
    expect((await get("hub.example")).status).toBe(200);
  });
});
