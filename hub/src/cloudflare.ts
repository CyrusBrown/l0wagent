// The cloudflared accelerator's calls to the Cloudflare API (protocol/accelerators.md). Plain fetch with a
// Bearer token; every answer is checked for `success`. Each id is saved the moment Cloudflare returns it
// (`save`), so a retry after a failure continues where the last attempt stopped.
import type { AcceleratorConfig } from "./config";
import type { Accel, AccelPatch } from "./db";

export class CfError extends Error {
  constructor(
    readonly status: number,
    readonly code: number | null,
    message: string,
  ) {
    super(message);
  }
}

export type Save = (patch: AccelPatch) => Promise<void>;

/** The hostname Cloudflare sees: no port. */
const bare = (host: string): string => host.split(":")[0].toLowerCase();

class Api {
  constructor(private cfg: AcceleratorConfig) {}

  /** One call; returns `result`. Throws CfError on a non-2xx status or `success: false`. */
  async call(method: string, path: string, body?: unknown): Promise<any> { // eslint-disable-line @typescript-eslint/no-explicit-any
    let res: Response;
    try {
      res = await fetch(this.cfg.apiBase + path, {
        method,
        headers: { authorization: `Bearer ${this.cfg.token}`, ...(body !== undefined ? { "content-type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(15000),
      });
    } catch (e) {
      throw new CfError(0, null, `Cloudflare unreachable: ${(e as Error).name}`);
    }
    let j: any = null; // eslint-disable-line @typescript-eslint/no-explicit-any
    try {
      j = await res.json();
    } catch {
      /* not JSON */
    }
    if (!res.ok || !j || j.success !== true) {
      const err = Array.isArray(j?.errors) ? j.errors[0] : undefined;
      throw new CfError(res.status, typeof err?.code === "number" ? err.code : null, String(err?.message ?? `HTTP ${res.status}`));
    }
    return j.result;
  }

  /** DELETE that treats "already gone" as done. */
  async remove(path: string): Promise<void> {
    try {
      await this.call("DELETE", path);
    } catch (e) {
      if (e instanceof CfError && e.status === 404) return;
      throw e;
    }
  }
}

export interface Names {
  member: string;
  instance: string;
  files: string;
}

const hex8 = (): string => [...crypto.getRandomValues(new Uint8Array(4))].map((b) => b.toString(16).padStart(2, "0")).join("");

async function dnsRecord(api: Api, zone: string, host: string, target: string): Promise<string> {
  const body = { type: "CNAME", name: host, content: target, proxied: true };
  try {
    return (await api.call("POST", `/zones/${zone}/dns_records`, body)).id;
  } catch (e) {
    if (!(e instanceof CfError) || !(e.code === 81053 || e.code === 81057 || e.code === 81058 || /already exists/i.test(e.message))) throw e;
  }
  // the record exists (an earlier attempt, or the owner's): point it at this tunnel
  const found = await api.call("GET", `/zones/${zone}/dns_records?name=${encodeURIComponent(host)}`);
  const id = Array.isArray(found) && found[0]?.id;
  if (!id) throw new CfError(409, null, "a DNS record exists but cannot be found");
  await api.call("PUT", `/zones/${zone}/dns_records/${id}`, body);
  return id;
}

async function workerRoute(api: Api, zone: string, host: string): Promise<string> {
  const body = { pattern: `${host}/*` }; // no script: this route negates the wildcard Worker route
  try {
    return (await api.call("POST", `/zones/${zone}/workers/routes`, body)).id;
  } catch (e) {
    if (!(e instanceof CfError) || !/already exists|duplicate/i.test(e.message)) throw e;
  }
  const found = await api.call("GET", `/zones/${zone}/workers/routes`);
  const id = Array.isArray(found) && found.find((r: { pattern?: string }) => r.pattern === body.pattern)?.id;
  if (!id) throw new CfError(409, null, "a Worker route exists but cannot be found");
  return id;
}

/** Make (or reuse) the tunnel, point it at `service`, create both DNS records. Returns the new state and the run token. */
export async function enable(cfg: AcceleratorConfig, n: Names, a: Accel, service: string, save: Save): Promise<{ accel: Accel; token: string }> {
  const api = new Api(cfg);
  const acct = `/accounts/${cfg.accountId}/cfd_tunnel`;
  const s = { ...a };
  const put = async (patch: AccelPatch) => {
    Object.assign(s, patch);
    await save(patch);
  };
  if (!s.tunnelId) {
    const t = await api.call("POST", acct, { name: `l0w-${n.member}-${hex8()}`, config_src: "cloudflare" });
    await put({ tunnelId: t.id });
  }
  const id = s.tunnelId as string;
  await api.call("PUT", `${acct}/${id}/configurations`, {
    config: { ingress: [{ hostname: bare(n.instance), service }, { hostname: bare(n.files), service }, { service: "http_status:404" }] },
  });
  if (s.service !== service) await put({ service });
  const target = `${id}.cfargotunnel.com`;
  if (!s.dnsInstance) await put({ dnsInstance: await dnsRecord(api, cfg.zoneId, bare(n.instance), target) });
  if (!s.dnsFiles) await put({ dnsFiles: await dnsRecord(api, cfg.filesZoneId, bare(n.files), target) });
  const token = await api.call("GET", `${acct}/${id}/token`);
  if (typeof token !== "string" || !token) throw new CfError(502, null, "Cloudflare gave no tunnel token");
  return { accel: s, token };
}

/** Send the hostnames through the tunnel. Throws NotConnected when the tunnel has no live connection. */
export class NotConnected extends Error {}

/** Whether Cloudflare sees at least one live `cloudflared` connection on the tunnel. */
export async function tunnelLive(cfg: AcceleratorConfig, tunnelId: string): Promise<boolean> {
  const t = await new Api(cfg).call("GET", `/accounts/${cfg.accountId}/cfd_tunnel/${tunnelId}`);
  return t?.status === "healthy" || t?.status === "degraded" || (Array.isArray(t?.connections) && t.connections.length > 0);
}

export async function activate(cfg: AcceleratorConfig, n: Names, a: Accel, save: Save): Promise<Accel> {
  const api = new Api(cfg);
  const s = { ...a };
  if (s.active && s.routeInstance && s.routeFiles) return s;
  if (!(await tunnelLive(cfg, s.tunnelId as string))) throw new NotConnected("the tunnel has no live connection");
  if (!s.routeInstance) {
    s.routeInstance = await workerRoute(api, cfg.zoneId, bare(n.instance));
    await save({ routeInstance: s.routeInstance });
  }
  if (!s.routeFiles) {
    s.routeFiles = await workerRoute(api, cfg.filesZoneId, bare(n.files));
    await save({ routeFiles: s.routeFiles });
  }
  s.active = true;
  s.downSince = null;
  await save({ active: true, downSince: null });
  return s;
}

/** Back to the spec tunnel: remove the two script-less routes. */
export async function deactivate(cfg: AcceleratorConfig, a: Accel, save: Save): Promise<Accel> {
  const api = new Api(cfg);
  const s = { ...a };
  if (s.active || s.downSince !== null) {
    s.active = false;
    s.downSince = null;
    await save({ active: false, downSince: null });
  }
  if (s.routeInstance) {
    await api.remove(`/zones/${cfg.zoneId}/workers/routes/${s.routeInstance}`);
    s.routeInstance = null;
    await save({ routeInstance: null });
  }
  if (s.routeFiles) {
    await api.remove(`/zones/${cfg.filesZoneId}/workers/routes/${s.routeFiles}`);
    s.routeFiles = null;
    await save({ routeFiles: null });
  }
  return s;
}

/** Deactivate, then delete the DNS records and the tunnel. */
export async function disable(cfg: AcceleratorConfig, a: Accel, save: Save): Promise<Accel> {
  const api = new Api(cfg);
  let s = await deactivate(cfg, a, save);
  if (s.dnsInstance) {
    await api.remove(`/zones/${cfg.zoneId}/dns_records/${s.dnsInstance}`);
    s = { ...s, dnsInstance: null };
    await save({ dnsInstance: null });
  }
  if (s.dnsFiles) {
    await api.remove(`/zones/${cfg.filesZoneId}/dns_records/${s.dnsFiles}`);
    s = { ...s, dnsFiles: null };
    await save({ dnsFiles: null });
  }
  if (s.tunnelId) {
    const t = `/accounts/${cfg.accountId}/cfd_tunnel/${s.tunnelId}`;
    await api.remove(`${t}/connections`);
    await api.remove(t);
    s = { ...s, tunnelId: null, service: null };
    await save({ tunnelId: null, service: null });
  }
  return s;
}
