// The Worker: routes by Host. A member's instance or files hostname goes down that member's tunnel;
// everything else is the hub (capabilities, the core API, the tunnel's WebSocket).
import { sweepAccelerators } from "./accelerator";
import { handleApi } from "./api";
import { ConfigError, type Config, loadConfig, matchHost } from "./config";
import { type Member, getMember, serviceExists } from "./db";
import type { Env } from "./env";
import { VERSION } from "./proto/consts";
import { errorResponse, jsonResponse, offlineResponse } from "./respond";
import { META, type Meta } from "./tunnel";

export { MemberRoute } from "./member";
export { TunnelConn } from "./tunnel";

const MEMBER_TTL_MS = 10_000;
const known = new Map<string, { m: Member; at: number }>();

/** Active tunnel members, remembered for a few seconds per isolate: a hot hostname costs no D1 read per
 * request. A member that leaves is refused by its MemberRoute at once; this only delays the 410. */
async function lookupMember(env: Env, name: string): Promise<Member | null> {
  const hit = known.get(name);
  const t = Date.now();
  if (hit && t - hit.at < MEMBER_TTL_MS) return hit.m;
  const m = await getMember(env.DB, name);
  if (m && m.status === "active") {
    if (known.size > 1000) known.clear();
    known.set(name, { m, at: t });
  } else {
    known.delete(name);
  }
  return m;
}

const isUpgrade = (r: Request): boolean => (r.headers.get("upgrade") ?? "").toLowerCase() === "websocket";

/** Answer without reading the request body. The body is cancelled so the runtime sends the answer
 * right away instead of waiting for an upload nobody will use (workerd holds an early response until
 * the body is consumed or cancelled). */
async function early(request: Request, res: Response): Promise<Response> {
  try {
    await request.body?.cancel();
  } catch {
    /* already closed or locked */
  }
  return res;
}

async function front(request: Request, env: Env, cfg: Config, member: Member, kind: "instance" | "files"): Promise<Response> {
  if (isUpgrade(request)) {
    return early(
      request,
      jsonResponse({ v: VERSION, error: "websocket_unsupported", detail: "WebSocket upgrades through the tunnel are not part of l0w/0.1" }, 501),
    );
  }
  if (member.status === "left") return early(request, errorResponse(410, "left", "this member has left", {}, { "cache-control": "no-store" }));
  const url = new URL(request.url);
  const limits = cfg.limits;
  const declared = Number.parseInt(request.headers.get("content-length") ?? "0", 10);
  if (Number.isFinite(declared) && declared > limits.request_body_bytes) {
    return early(request, errorResponse(413, "request_too_large", "request body over the hub's limit"));
  }
  const route = await env.MEMBER.get(env.MEMBER.idFromName(member.name)).route(url.pathname.startsWith("/api/auth/"), limits.auth_rate_per_minute);
  switch (route.s) {
    case "gone":
      return early(request, errorResponse(410, "left", "this member has left", {}, { "cache-control": "no-store" }));
    case "limited":
      return early(request, errorResponse(429, "rate_limited", "too many attempts", {}, { "retry-after": "60", "cache-control": "no-store" }));
    case "offline":
      return early(request, offlineResponse(request.headers.get("accept") ?? ""));
  }
  // the client address is what Cloudflare saw; X-Forwarded-For and the like are never believed
  const meta: Meta = {
    name: member.name,
    kind,
    host: member.hostnames[kind],
    client: request.headers.get("cf-connecting-ip") ?? "0.0.0.0",
    scheme: url.protocol.slice(0, -1),
  };
  const headers = new Headers(request.headers);
  headers.set(META, JSON.stringify(meta));
  const stub = env.CONN.get(env.CONN.idFromString(route.conn));
  // the signal carries the visitor's disconnect through to the tunnel, which then resets the stream
  return stub.fetch(new Request(request, { headers, redirect: "manual", signal: request.signal }));
}

async function tunnelSocket(request: Request, env: Env): Promise<Response> {
  if (!isUpgrade(request)) return errorResponse(426, "upgrade_required", "this endpoint is the tunnel's WebSocket");
  let req = request;
  if (request.headers.has(META)) {
    const h = new Headers(request.headers);
    h.delete(META);
    req = new Request(request, { headers: h });
  }
  // A random name per connection: the socket stays in this Durable Object for its whole life, and
  // MemberRoute records which one is the member's current connection once the hello verifies.
  return env.CONN.get(env.CONN.newUniqueId()).fetch(req);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    let cfg: Config;
    try {
      cfg = loadConfig(env);
    } catch (e) {
      if (e instanceof ConfigError) {
        console.error("hub configuration error:", e.message);
        return errorResponse(500, "misconfigured", "the hub is not configured");
      }
      throw e;
    }
    const url = new URL(request.url);
    const host = url.host.toLowerCase();
    if (host !== cfg.baseHost && host !== cfg.tunnelHost) {
      const hit = matchHost(cfg, host);
      if (hit) {
        const member = await lookupMember(env, hit.name);
        if (member && member.hostnames[hit.kind].toLowerCase() === host) {
          return front(request, env, cfg, member, hit.kind);
        }
        // A hub service's hostname is answered by the service's own Worker (a more specific Cloudflare route);
        // if a request gets here instead, say so rather than treating it as a member or the hub's API.
        if (!member && (await serviceExists(env.DB, hit.name))) {
          return early(request, errorResponse(404, "service_not_here", "this is a hub service's hostname; it is served by the service itself"));
        }
      }
    }
    if (url.pathname === "/v0/tunnel") return tunnelSocket(request, env);
    return handleApi(request, env, cfg);
  },

  // The cron trigger (wrangler.toml `[triggers] crons`): watch active accelerators (protocol/accelerators.md).
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    let cfg: Config;
    try {
      cfg = loadConfig(env);
    } catch (e) {
      console.error("hub configuration error:", (e as Error).message);
      return;
    }
    ctx.waitUntil(sweepAccelerators(env, cfg));
  },
} satisfies ExportedHandler<Env>;
