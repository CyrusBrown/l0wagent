#!/usr/bin/env node
// A mock of the few Cloudflare API endpoints the cloudflared accelerator uses (src/cloudflare.ts), in memory,
// for local runs and the conformance suite. Tunnels report status "healthy". GET /__state dumps everything.
//   node scripts/mock-cf-api.mjs [--port 8788]
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";

const i = process.argv.indexOf("--port");
const port = Number(i > 0 ? process.argv[i + 1] : process.env.MOCK_CF_PORT || 8788);

const tunnels = new Map(); // id -> {id, name, config, connections}
const dns = new Map(); // id -> {zone, ...record}
const routes = new Map(); // id -> {zone, id, pattern}
const TOKEN = process.env.MOCK_CF_TOKEN || ""; // when set, the Bearer token must match

const ok = (res, result, status = 200) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ success: true, errors: [], messages: [], result }));
};
const fail = (res, status, code, message) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify({ success: false, errors: [{ code, message }], messages: [], result: null }));
};

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const t = Buffer.concat(chunks).toString();
  return t ? JSON.parse(t) : {};
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://mock");
    const path = url.pathname;
    if (path === "/__state") return ok(res, { tunnels: [...tunnels.values()], dns: [...dns.values()], routes: [...routes.values()] });
    if (TOKEN && req.headers.authorization !== `Bearer ${TOKEN}`) return fail(res, 401, 10000, "Authentication error");
    const body = req.method === "POST" || req.method === "PUT" ? await readJson(req) : {};
    let m;
    if ((m = /^\/accounts\/([^/]+)\/cfd_tunnel$/.exec(path)) && req.method === "POST") {
      if (typeof body.name !== "string" || !body.name) return fail(res, 400, 1001, "name required");
      const t = { id: randomUUID(), name: body.name, config_src: body.config_src, config: null, connections: [], status: "healthy" };
      tunnels.set(t.id, t);
      return ok(res, t);
    }
    if ((m = /^\/accounts\/([^/]+)\/cfd_tunnel\/([^/]+)(?:\/(configurations|token|connections))?$/.exec(path))) {
      const t = tunnels.get(m[2]);
      if (!t) return fail(res, 404, 1003, "tunnel not found");
      const sub = m[3];
      if (!sub && req.method === "GET") return ok(res, t);
      if (!sub && req.method === "DELETE") {
        tunnels.delete(t.id);
        return ok(res, t);
      }
      if (sub === "configurations" && req.method === "PUT") {
        if (!Array.isArray(body.config?.ingress)) return fail(res, 400, 1002, "ingress required");
        t.config = body.config;
        return ok(res, { tunnel_id: t.id, config: t.config });
      }
      if (sub === "token" && req.method === "GET") return ok(res, Buffer.from(JSON.stringify({ a: m[1], t: t.id, s: "mock" })).toString("base64"));
      if (sub === "connections" && req.method === "DELETE") return ok(res, null);
    }
    if ((m = /^\/zones\/([^/]+)\/dns_records(?:\/([^/]+))?$/.exec(path))) {
      const zone = m[1];
      if (!m[2] && req.method === "GET") {
        const name = url.searchParams.get("name");
        return ok(res, [...dns.values()].filter((r) => r.zone === zone && (!name || r.name === name)));
      }
      if (!m[2] && req.method === "POST") {
        if ([...dns.values()].some((r) => r.zone === zone && r.name === body.name)) return fail(res, 400, 81057, "Record already exists.");
        const r = { id: randomUUID(), zone, type: body.type, name: body.name, content: body.content, proxied: body.proxied };
        dns.set(r.id, r);
        return ok(res, r);
      }
      const r = dns.get(m[2] ?? "");
      if (!r || r.zone !== zone) return fail(res, 404, 81044, "Record does not exist.");
      if (req.method === "PUT") {
        Object.assign(r, { type: body.type, name: body.name, content: body.content, proxied: body.proxied });
        return ok(res, r);
      }
      if (req.method === "DELETE") {
        dns.delete(r.id);
        return ok(res, { id: r.id });
      }
    }
    if ((m = /^\/zones\/([^/]+)\/workers\/routes(?:\/([^/]+))?$/.exec(path))) {
      const zone = m[1];
      if (!m[2] && req.method === "GET") return ok(res, [...routes.values()].filter((r) => r.zone === zone));
      if (!m[2] && req.method === "POST") {
        if ([...routes.values()].some((r) => r.zone === zone && r.pattern === body.pattern)) return fail(res, 409, 10020, "Route already exists.");
        const r = { id: randomUUID(), zone, pattern: body.pattern };
        if (body.script) r.script = body.script;
        routes.set(r.id, r);
        return ok(res, r);
      }
      const r = routes.get(m[2] ?? "");
      if (!r || r.zone !== zone) return fail(res, 404, 10007, "Route not found.");
      if (req.method === "DELETE") {
        routes.delete(r.id);
        return ok(res, { id: r.id });
      }
    }
    return fail(res, 404, 7003, `no mock for ${req.method} ${path}`);
  } catch (e) {
    return fail(res, 500, 9999, String(e?.message ?? e));
  }
});

server.listen(port, "127.0.0.1", () => console.log(`mock Cloudflare API on http://127.0.0.1:${port}`));
