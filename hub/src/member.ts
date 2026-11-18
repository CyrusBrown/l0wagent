// MemberRoute: one Durable Object per member name. It is the single place that knows which
// TunnelConn currently holds that member's tunnel, so "one live connection per member" and "a new
// verified connection replaces the old one" are decided here, one request at a time. It also keeps
// the member's /api/auth/* sliding-minute counter.
import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import { SlidingWindow } from "./queue";

export type RouteResult = { s: "ok"; conn: string } | { s: "limited" } | { s: "offline" } | { s: "gone" };

interface Saved {
  conn: string | null;
  left: boolean;
  hits: number[];
}

export class MemberRoute extends DurableObject<Env> {
  private conn: string | null = null;
  private left = false;
  private window = new SlidingWindow();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      const s = await ctx.storage.get<Saved>("s");
      if (s) {
        this.conn = s.conn;
        this.left = s.left;
        this.window = new SlidingWindow(s.hits);
      }
    });
  }

  private save(): void {
    void this.ctx.storage.put("s", { conn: this.conn, left: this.left, hits: this.window.hits } satisfies Saved);
  }

  private stub(id: string) {
    return this.env.CONN.get(this.env.CONN.idFromString(id));
  }

  /** Called by the Worker for every visitor request to this member's hostnames. */
  async route(isAuth: boolean, authLimit: number): Promise<RouteResult> {
    if (this.left) return { s: "gone" };
    if (isAuth) {
      const ok = this.window.allow(authLimit, Date.now());
      this.save();
      if (!ok) return { s: "limited" };
    }
    return this.conn ? { s: "ok", conn: this.conn } : { s: "offline" };
  }

  /** A TunnelConn whose handshake verified. The previous connection (if any) stops taking new
   * streams and closes 4409 once its open streams end (at most 30 s). False if the member left. */
  async register(connId: string): Promise<boolean> {
    if (this.left) return false;
    const prev = this.conn;
    this.conn = connId;
    this.save();
    if (prev && prev !== connId) {
      try {
        await this.stub(prev).drain();
      } catch {
        /* the old connection is already gone */
      }
    }
    return true;
  }

  /** A TunnelConn closed (or found it has no socket). Only clears the route if it is still the current one. */
  unregister(connId: string): void {
    if (this.conn === connId) {
      this.conn = null;
      this.save();
    }
  }

  /** Close the live tunnel (leave, re-registration). `permanent`: the member left, nothing registers again. */
  async drop(code: number, permanent: boolean): Promise<void> {
    const prev = this.conn;
    this.conn = null;
    if (permanent) this.left = true;
    this.save();
    if (prev) {
      try {
        await this.stub(prev).closeNow(code);
      } catch {
        /* already gone */
      }
    }
  }
}
