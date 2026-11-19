// The cloudflared accelerator's endpoint: POST /v0/accelerators/cloudflared (protocol/accelerators.md).
import { HubError } from "./api";
import * as b64 from "./proto/b64";
import * as cf from "./cloudflare";
import type { AcceleratorConfig, Config } from "./config";
import * as db from "./db";
import type { Env } from "./env";
import * as docs from "./proto/docs";
import { ProtocolError, UnsupportedVersion } from "./proto/errors";
import { Key } from "./proto/keys";
import { verifyAcceleratorRequest } from "./proto/payloads";
import { parseTime } from "./proto/times";
import { jsonResponse } from "./respond";

/** The capabilities extra, or null when the accelerator is off. */
export function acceleratorExtra(cfg: Config): Record<string, unknown> | null {
  const a = cfg.accelerator;
  return a ? { kind: "accelerator", id: "cloudflared", doc: a.doc, api: a.api } : null;
}

function stateBody(m: db.Member, a: db.Accel, token?: string): Record<string, unknown> {
  const state: Record<string, unknown> = {
    enabled: a.tunnelId !== null,
    active: a.active,
    tunnel_id: a.tunnelId,
    hostnames: [m.hostnames.instance, m.hostnames.files],
  };
  if (token !== undefined) state.token = token;
  return { state };
}

const upstream = (e: unknown): HubError => {
  console.error("cloudflare accelerator call failed:", e instanceof cf.CfError ? `${e.status} ${e.code ?? ""} ${e.message}` : e);
  return new HubError(502, "upstream", "the hub's provider failed; try again shortly", {}, { "retry-after": "30" });
};

export async function handleAccelerator(request: Request, env: Env, cfg: Config, body: docs.Obj): Promise<Response> {
  const acc = cfg.accelerator;
  if (!acc) throw new HubError(404, "not_found", "no such endpoint");
  // the member named in the (not yet verified) payload decides which key checks the signature
  let name: string;
  try {
    const d = docs.shape(body);
    // only the address is read here (to find the member's key); iat, signature and the rest are checked below
    const p = JSON.parse(b64.fromUtf8(b64.decode(d.payload)));
    if (!docs.isObject(p) || typeof p.address !== "string") throw new Error("payload must have an address");
    const [n, domain] = p.address.toLowerCase().split("@");
    if (!n || domain === undefined) throw new Error("address");
    if (domain !== cfg.domain.toLowerCase()) throw new HubError(401, "bad_sig", "request is for another address");
    name = n;
  } catch (e) {
    if (e instanceof HubError) throw e;
    if (e instanceof UnsupportedVersion) throw new HubError(400, "unsupported_version", e.message);
    throw new HubError(400, "bad_request", (e as Error).message || "malformed request");
  }
  const m = await db.getMember(env.DB, name);
  if (!m || m.status !== "active") throw new HubError(403, "not_member", "no active member with that address");
  let p: docs.Obj;
  try {
    p = await verifyAcceleratorRequest(body, await Key.fromJSON(m.keys.sign), { hub: cfg.domain, address: m.address });
  } catch (e) {
    if (e instanceof ProtocolError) throw new HubError(e.code === "bad_sig" ? 401 : 400, e.code === "bad_sig" ? "bad_sig" : "bad_request", e.detail);
    throw e;
  }
  if (m.mode !== "tunnel") throw new HubError(403, "not_member", "only members that use the hub's tunnel can use an accelerator");
  const iat = parseTime(p.iat);
  if (!(await db.takeAccelIat(env.DB, name, iat))) {
    const last = (await db.getAccel(env.DB, name)).lastIat;
    throw new HubError(409, "stale", "iat must be later than the last accepted request", { last_iat: new Date(last * 1000).toISOString().replace(/\.\d{3}Z$/, "Z") });
  }
  const names: cf.Names = { member: name, instance: m.hostnames.instance, files: m.hostnames.files };
  const save: cf.Save = (patch) => db.saveAccel(env.DB, name, patch);
  const a = await db.getAccel(env.DB, name);
  try {
    switch (p.action) {
      case "enable": {
        const r = await cf.enable(acc, names, a, p.service, save);
        return jsonResponse(stateBody(m, r.accel, r.token), 200, { "cache-control": "no-store" });
      }
      case "activate": {
        if (!a.tunnelId) throw new HubError(409, "not_enabled", "enable the accelerator first");
        return jsonResponse(stateBody(m, await cf.activate(acc, names, a, save)), 200, { "cache-control": "no-store" });
      }
      case "deactivate": {
        if (!a.tunnelId) throw new HubError(409, "not_enabled", "enable the accelerator first");
        return jsonResponse(stateBody(m, await cf.deactivate(acc, a, save)), 200, { "cache-control": "no-store" });
      }
      default:
        return jsonResponse(stateBody(m, await cf.disable(acc, a, save)), 200, { "cache-control": "no-store" });
    }
  } catch (e) {
    if (e instanceof HubError) throw e;
    if (e instanceof cf.NotConnected) throw new HubError(409, "not_connected", "the tunnel has no live connection yet");
    throw upstream(e);
  }
}

/** Leaving and re-registration: take the member's accelerator down. Best effort: a failure is logged, never blocks. */
export async function disableForMember(env: Env, cfg: Config, name: string): Promise<void> {
  const acc = cfg.accelerator;
  if (!acc) return;
  try {
    const a = await db.getAccel(env.DB, name);
    if (!a.tunnelId && !a.dnsInstance && !a.dnsFiles && !a.routeInstance && !a.routeFiles) return;
    await cf.disable(acc, a, (patch) => db.saveAccel(env.DB, name, patch));
  } catch (e) {
    console.error(`could not disable ${name}'s accelerator:`, e instanceof cf.CfError ? `${e.status} ${e.message}` : e);
  }
}

/** One member in the sweep: a live tunnel clears the mark, a dead one is marked, and one dead for `downAfter`
 * seconds is deactivated (its visitors then get the spec tunnel's answer: the instance, or the offline page).
 * Returns what happened. */
export async function sweepOne(acc: AcceleratorConfig, a: db.Accel, now: number, downAfter: number, save: cf.Save): Promise<"live" | "marked" | "waiting" | "deactivated"> {
  if (!a.active || !a.tunnelId) return "live";
  if (await cf.tunnelLive(acc, a.tunnelId)) {
    if (a.downSince !== null) await save({ downSince: null });
    return "live";
  }
  if (a.downSince === null) {
    await save({ downSince: now });
    return "marked";
  }
  if (now - a.downSince < downAfter) return "waiting";
  await cf.deactivate(acc, a, save);
  return "deactivated";
}

/** The scheduled sweep (protocol/accelerators.md, "Watching active tunnels"). One member's failure never stops the rest. */
export async function sweepAccelerators(env: Env, cfg: Config, now = Math.floor(Date.now() / 1000)): Promise<void> {
  const acc = cfg.accelerator;
  if (!acc) return;
  for (const a of await db.listActiveAccels(env.DB)) {
    try {
      const r = await sweepOne(acc, a, now, cfg.accelDownAfter, (patch) => db.saveAccel(env.DB, a.name, patch));
      if (r === "deactivated") console.log(`accelerator: ${a.name}'s tunnel had no connection for ${cfg.accelDownAfter}s or more; hostnames moved back to the spec tunnel`);
    } catch (e) {
      console.error(`accelerator sweep for ${a.name} failed:`, e instanceof cf.CfError ? `${e.status} ${e.code ?? ""} ${e.message}` : e);
    }
  }
}
