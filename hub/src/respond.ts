// Small response helpers shared by the Worker and the Durable Objects.
import { VERSION } from "./proto/consts";

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

export function errorResponse(
  status: number,
  code: string,
  detail = "",
  extra: Record<string, unknown> = {},
  headers: Record<string, string> = {},
): Response {
  return jsonResponse({ v: VERSION, error: code, detail, ...extra }, status, headers);
}

/** Does this `Accept` header prefer JSON over HTML? (tunnel.md, "What the hub answers") */
export function prefersJson(accept: string): boolean {
  const best: Record<"json" | "html", [number, number]> = { json: [-1, 0], html: [-1, 0] };
  accept.split(",").forEach((part, i) => {
    const bits = part.split(";").map((b) => b.trim());
    const mt = bits[0].toLowerCase();
    let q = 1;
    for (const b of bits.slice(1)) {
      if (b.startsWith("q=")) {
        const n = Number(b.slice(2));
        q = Number.isNaN(n) ? 0 : n;
      }
    }
    const kind = mt === "application/json" || mt === "application/*" ? "json" : mt === "text/html" || mt === "application/xhtml+xml" ? "html" : null;
    if (kind) {
      const cur = best[kind];
      if (q > cur[0] || (q === cur[0] && -i > cur[1])) best[kind] = [q, -i];
    }
  });
  const [j, h] = [best.json, best.html];
  return j[0] > h[0] || (j[0] === h[0] && j[1] > h[1]);
}

/** 502 with Retry-After: 30; JSON when the request prefers it, else a short HTML page. Never cached. */
export function offlineResponse(accept: string): Response {
  const extra = { "retry-after": "30", "cache-control": "no-store" };
  if (prefersJson(accept)) return jsonResponse({ v: VERSION, error: "instance_offline" }, 502, extra);
  const html = "<!doctype html><title>Offline</title><h1>This instance is offline</h1><p>Try again in a little while.</p>";
  return new Response(html, { status: 502, headers: { "content-type": "text/html; charset=utf-8", ...extra } });
}
