// TunnelConn: one Durable Object per tunnel connection (named by a random id), holding the
// instance's WebSocket for its whole life. The WebSocket is accepted here, challenged, verified
// against the member's key in D1, and then registered with the member's MemberRoute, which makes
// it the member's current connection. Visitor requests arrive here by fetch() from the Worker (with
// the x-l0w-tunnel-meta header) and are carried over the socket as frames with per-stream flow control.
import { DurableObject } from "cloudflare:workers";
import { ConfigError, type Config, loadConfig, releaseLess } from "./config";
import { getMember } from "./db";
import type { Env } from "./env";
import * as b64 from "./proto/b64";
import { parsePayload } from "./proto/docs";
import { Key } from "./proto/keys";
import { splitAddress, shapeOk, verifyTunnelHello } from "./proto/payloads";
import * as lt from "./proto/tunnel";
import { AsyncQueue } from "./queue";
import { errorResponse, offlineResponse } from "./respond";

/** Set by the Worker on visitor requests (and removed from upgrade requests). Starts with
 * x-l0w-tunnel- so the header filter drops it before OPEN. */
export const META = "x-l0w-tunnel-meta";

export interface Meta {
  name: string;
  kind: "instance" | "files";
  host: string;
  client: string;
  scheme: string;
}

const HOP_RESPONSE = new Set(["connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"]);
const HANDSHAKE_TIMEOUT_MS = 10_000;
const NONCE_MAX_AGE_MS = 30_000;
const PING_AFTER_MS = 30_000;
const DEAD_AFTER_MS = 90_000;
const DRAIN_MS = 30_000;
const TICK_MS = 5_000;
const CREDIT_STEP = 16_384;

const END = Symbol("end");
const ABORT = Symbol("abort");
type Chunk = Uint8Array | typeof END | typeof ABORT;

type Head =
  | { k: "response"; status: number; headers: [string, string][] }
  | { k: "offline" }
  | { k: "too_large" }
  | { k: "gone" };

class Stream {
  head: Promise<Head>;
  private resolveHead!: (h: Head) => void;
  headDone = false;
  queue = new AsyncQueue<Chunk>();
  sendWindow = lt.INITIAL_WINDOW; // what the hub may still send (request body)
  recvCredit = lt.INITIAL_WINDOW; // what the instance may still send (response body)
  private windowWaiters: Array<() => void> = [];
  reset = false;
  requestEnded = false;
  responseEnded = false;
  released = false;
  visitorGone = false;
  reqReader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  constructor(readonly id: number) {
    this.head = new Promise((r) => (this.resolveHead = r));
  }

  resolve(h: Head): void {
    if (!this.headDone) {
      this.headDone = true;
      this.resolveHead(h);
    }
  }

  waitWindow(): Promise<void> {
    return new Promise((r) => this.windowWaiters.push(r));
  }

  wake(): void {
    const w = this.windowWaiters;
    this.windowWaiters = [];
    for (const f of w) f();
  }

  /** The stream ends without a normal END (RESET, or the connection closed). */
  abort(): void {
    this.reset = true;
    this.wake();
    if (!this.headDone) this.resolve({ k: "offline" });
    else this.queue.push(ABORT);
  }
}

class Session {
  streams = new Map<number, Stream>();
  nextId = 1;
  draining = false;
  closed = false;
  lastRecv = Date.now();
  lastPing = 0;
  constructor(
    readonly name: string,
    readonly id: string,
  ) {}
  get usable(): boolean {
    return !this.draining && !this.closed;
  }
}

function safeClose(ws: WebSocket, code: number): void {
  try {
    ws.close(code);
    return;
  } catch {
    /* the runtime refused this code; fall back to a normal close */
  }
  try {
    ws.close(1000);
  } catch {
    /* already closed */
  }
}

export class TunnelConn extends DurableObject<Env> {
  private ws: WebSocket | null = null;
  private phase: "new" | "hello" | "verifying" | "open" | "closed" = "new";
  private nonce: Uint8Array = new Uint8Array(32);
  private nonceAt = 0;
  private session: Session | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private drainTimer: ReturnType<typeof setTimeout> | null = null;
  private cfg!: Config;

  // --- entry point --------------------------------------------------------------------------

  async fetch(request: Request): Promise<Response> {
    try {
      this.cfg = loadConfig(this.env);
    } catch (e) {
      if (e instanceof ConfigError) return errorResponse(500, "misconfigured", "the hub is not configured");
      throw e;
    }
    const meta = request.headers.get(META);
    if (meta === null) return this.upgrade(request);
    return this.proxy(request, JSON.parse(meta) as Meta);
  }

  // --- the instance's side: handshake and frames --------------------------------------------

  private upgrade(request: Request): Response {
    if ((request.headers.get("upgrade") ?? "").toLowerCase() !== "websocket") {
      return errorResponse(426, "upgrade_required", "this endpoint is the tunnel's WebSocket");
    }
    const offered = (request.headers.get("sec-websocket-protocol") ?? "").split(",").map((s) => s.trim());
    if (!offered.includes(lt.SUBPROTOCOL)) return errorResponse(400, "bad_subprotocol", `the subprotocol ${lt.SUBPROTOCOL} is required`);
    if (this.ws) return errorResponse(409, "conflict", "this connection is already in use");

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair) as [WebSocket, WebSocket];
    server.accept();
    server.binaryType = "arraybuffer";
    this.ws = server;
    this.phase = "hello";
    this.nonce = crypto.getRandomValues(new Uint8Array(32));
    this.nonceAt = Date.now();
    server.send(lt.challengeMessage(this.nonce, this.cfg.domain));
    const t = setTimeout(() => {
      if (this.phase === "hello") this.refuse();
    }, HANDSHAKE_TIMEOUT_MS);
    server.addEventListener("message", (ev: MessageEvent) => {
      this.onMessage(ev.data as string | ArrayBuffer).catch(() => this.refuse());
    });
    const gone = () => {
      clearTimeout(t);
      this.onClosed();
    };
    server.addEventListener("close", gone);
    server.addEventListener("error", gone);
    return new Response(null, { status: 101, webSocket: client, headers: { "sec-websocket-protocol": lt.SUBPROTOCOL } });
  }

  /** A failed handshake closes with 4401 and no detail. */
  private refuse(code = lt.CLOSE_UNAUTHORIZED): void {
    this.phase = "closed";
    if (this.ws) safeClose(this.ws, code);
  }

  private async onMessage(data: string | ArrayBuffer): Promise<void> {
    if (this.phase === "hello") {
      this.phase = "verifying";
      if (typeof data !== "string") return this.refuse();
      return this.onHello(data);
    }
    if (this.phase !== "open" || !this.session || !this.ws) {
      if (this.phase === "verifying") this.refuse(); // nothing is allowed before `ready`
      return;
    }
    this.session.lastRecv = Date.now();
    if (typeof data === "string") return void this.closeSocket(1003); // text after ready is not part of the protocol
    let f: lt.Frame;
    try {
      f = lt.decode(new Uint8Array(data));
    } catch {
      return void this.closeSocket(1002);
    }
    this.handle(this.session, f);
  }

  private async onHello(text: string): Promise<void> {
    const cfg = this.cfg;
    let name: string;
    let release: string;
    try {
      const doc = JSON.parse(text);
      const p = parsePayload(shapeOk(doc));
      let domain: string;
      [name, domain] = splitAddress(p.address);
      if (domain !== cfg.domain.toLowerCase()) return this.refuse();
      const m = await getMember(this.env.DB, name);
      if (!m || m.status !== "active" || m.mode !== "tunnel") return this.refuse();
      const hello = await verifyTunnelHello(doc, await Key.fromJSON(m.keys.sign), { nonce: b64.encode(this.nonce), hub: cfg.domain });
      if (Date.now() - this.nonceAt > NONCE_MAX_AGE_MS) return this.refuse();
      release = hello.release;
    } catch {
      return this.refuse();
    }
    if (releaseLess(release, cfg.minRelease)) return this.refuse(lt.CLOSE_UPGRADE_REQUIRED);
    if (!this.ws || this.phase !== "verifying") return;

    const sid = b64.encode(crypto.getRandomValues(new Uint8Array(9)));
    const session = new Session(name, sid);
    this.ws.send(lt.readyMessage(sid, cfg.limits));
    this.session = session;
    this.phase = "open";
    this.timer = setInterval(() => this.tick(session), TICK_MS);
    const ok = await this.env.MEMBER.get(this.env.MEMBER.idFromName(name)).register(this.ctx.id.toString());
    if (!ok) this.refuse(); // the member left while we were verifying
  }

  private closeSocket(code: number): void {
    if (this.ws) safeClose(this.ws, code);
  }

  private onClosed(): void {
    const s = this.session;
    this.phase = "closed";
    if (this.timer) clearInterval(this.timer);
    if (this.drainTimer) clearTimeout(this.drainTimer);
    this.timer = this.drainTimer = null;
    if (s && !s.closed) {
      s.closed = true;
      for (const st of [...s.streams.values()]) st.abort();
      this.ctx.waitUntil(this.env.MEMBER.get(this.env.MEMBER.idFromName(s.name)).unregister(this.ctx.id.toString()));
    }
  }

  private send(f: lt.Frame): void {
    if (!this.ws || this.session?.closed) return;
    try {
      this.ws.send(lt.encode(f));
    } catch {
      /* the socket is going away; onClosed cleans up */
    }
  }

  /** Liveness (tunnel.md): PING after 30 s of silence, dead after 90 s. */
  private tick(s: Session): void {
    const now = Date.now();
    const idle = now - s.lastRecv;
    if (idle > DEAD_AFTER_MS) {
      this.closeSocket(1001);
      this.onClosed();
    } else if (idle >= PING_AFTER_MS && now - s.lastPing >= PING_AFTER_MS - 1000) {
      s.lastPing = now;
      this.send(lt.pingFrame(crypto.getRandomValues(new Uint8Array(8))));
    }
  }

  private handle(s: Session, f: lt.Frame): void {
    if (f.type === lt.PING) {
      if (!lt.isPong(f)) this.send(lt.pongFor(f));
      return;
    }
    const st = s.streams.get(f.streamId);
    if (!st) return; // a stream we already finished; late frames are ignored
    switch (f.type) {
      case lt.RESPONSE: {
        const j = lt.frameJson(f);
        st.resolve({ k: "response", status: j.status, headers: j.headers });
        break;
      }
      case lt.DATA:
        st.recvCredit -= f.payload.length;
        if (st.recvCredit < 0) {
          this.send(lt.resetFrame(st.id, "flow_control"));
          st.abort();
        } else if (f.payload.length) {
          st.queue.push(f.payload);
        }
        break;
      case lt.END:
        st.responseEnded = true;
        st.queue.push(END);
        break;
      case lt.RESET:
        st.abort();
        break;
      case lt.CREDIT:
        st.sendWindow += lt.creditAmount(f);
        st.wake();
        break;
    }
  }

  // --- called by MemberRoute ----------------------------------------------------------------

  /** A newer connection replaced this one: no new streams; close 4409 once open streams end (at most 30 s). */
  drain(): void {
    const s = this.session;
    if (!s || s.closed) return;
    s.draining = true;
    if (!this.drainTimer) this.drainTimer = setTimeout(() => this.finishDrain(true), DRAIN_MS);
    this.finishDrain(false);
  }

  private finishDrain(force: boolean): void {
    const s = this.session;
    if (!s || !s.draining || s.closed) return;
    if (force || s.streams.size === 0) {
      this.closeSocket(lt.CLOSE_REPLACED);
      this.onClosed();
    }
  }

  /** Leave or re-registration: close now. */
  closeNow(code: number): void {
    this.closeSocket(code);
    this.onClosed();
  }

  // --- visitors -----------------------------------------------------------------------------

  private async proxy(request: Request, meta: Meta): Promise<Response> {
    const cfg = this.cfg;
    const accept = request.headers.get("accept") ?? "";
    const s = this.session;
    if (!s || !s.usable || this.phase !== "open") {
      if (!s) this.ctx.waitUntil(this.env.MEMBER.get(this.env.MEMBER.idFromName(meta.name)).unregister(this.ctx.id.toString()));
      return offlineResponse(accept);
    }
    if (s.streams.size >= cfg.limits.tunnel_streams) {
      return errorResponse(503, "too_many_streams", "too many open streams", {}, { "retry-after": "1", "cache-control": "no-store" });
    }
    if (s.nextId > 0xfffffff0) {
      this.closeSocket(1000); // out of stream ids: the instance reconnects
      return offlineResponse(accept);
    }
    const sid = s.nextId;
    s.nextId += 2;
    const st = new Stream(sid);
    s.streams.set(sid, st);

    const url = new URL(request.url);
    const headers = lt.filterRequestHeaders(request.headers as unknown as Iterable<[string, string]>).filter(([k]) => k !== "host");
    headers.unshift(["host", meta.host]);
    this.send(lt.openFrame(sid, request.method, url.pathname + url.search, meta.host, headers, meta.client, meta.scheme));

    const onAbort = () => {
      // the visitor went away: before the response head, answer nothing; after it, reset the stream
      st.visitorGone = true;
      st.resolve({ k: "gone" });
      this.release(s, st);
    };
    request.signal.addEventListener("abort", onAbort);
    void this.pumpRequest(s, st, request.body, cfg.limits.request_body_bytes).catch(() => this.release(s, st));

    const head = await st.head;
    if (head.k === "offline") {
      this.release(s, st);
      return offlineResponse(accept);
    }
    if (head.k === "too_large") {
      this.release(s, st);
      return errorResponse(413, "request_too_large", "request body over the hub's limit");
    }
    if (head.k === "gone") {
      this.release(s, st);
      return new Response(null, { status: 499 });
    }
    return this.respond(s, st, head, request.method === "HEAD");
  }

  private respond(s: Session, st: Stream, head: Extract<Head, { k: "response" }>, isHead: boolean): Response {
    const h = new Headers();
    for (const [k, v] of head.headers) {
      if (HOP_RESPONSE.has(k) || k.startsWith("proxy-")) continue;
      try {
        h.append(k, v);
      } catch {
        /* a header value the runtime cannot carry */
      }
    }
    const bodyless = isHead || head.status === 204 || head.status === 205 || head.status === 304;
    try {
      if (bodyless) {
        void this.discardBody(s, st);
        return new Response(null, { status: head.status, headers: h, encodeBody: "manual" });
      }
      let owed = 0;
      const body = new ReadableStream<Uint8Array>(
        {
          pull: async (ctrl) => {
            const item = await st.queue.shift();
            if (item === END) {
              ctrl.close();
              this.release(s, st);
            } else if (item === ABORT) {
              ctrl.error(new Error("the instance reset the stream"));
              this.release(s, st);
            } else {
              ctrl.enqueue(item);
              owed += item.length;
              if (owed >= CREDIT_STEP && !st.reset) {
                st.recvCredit += owed;
                this.send(lt.creditFrame(st.id, owed));
                owed = 0;
              }
            }
          },
          cancel: () => {
            st.visitorGone = true;
            this.release(s, st);
          },
        },
        new CountQueuingStrategy({ highWaterMark: 1 }),
      );
      return new Response(body, { status: head.status, headers: h, encodeBody: "manual" });
    } catch {
      // a status the runtime cannot carry (1xx): the instance misbehaved
      this.send(lt.resetFrame(st.id, "bad_response"));
      st.reset = true;
      this.release(s, st);
      return errorResponse(502, "bad_gateway", "the instance sent a response the hub cannot pass on");
    }
  }

  /** HEAD and bodiless statuses: wait for END and drop any DATA. */
  private async discardBody(s: Session, st: Stream): Promise<void> {
    for (;;) {
      const item = await st.queue.shift();
      if (item === END || item === ABORT) break;
    }
    this.release(s, st);
  }

  private release(s: Session, st: Stream): void {
    if (st.released) return;
    st.released = true;
    st.wake();
    if (st.reqReader && !st.requestEnded) void st.reqReader.cancel().catch(() => undefined);
    if (!(st.requestEnded && st.responseEnded) && !st.reset && !s.closed) this.send(lt.resetFrame(st.id, "cancelled"));
    s.streams.delete(st.id);
    this.finishDrain(false);
  }

  /** The visitor's request body as DATA frames, within the stream's window. */
  private async pumpRequest(s: Session, st: Stream, body: ReadableStream<Uint8Array> | null, limit: number): Promise<void> {
    if (body) {
      const reader = body.getReader();
      st.reqReader = reader;
      let total = 0;
      for (;;) {
        const r = await reader.read();
        if (r.done) break;
        const chunk = r.value;
        total += chunk.length;
        if (total > limit) {
          this.send(lt.resetFrame(st.id, "request_too_large"));
          st.reset = true;
          st.resolve({ k: "too_large" });
          void reader.cancel().catch(() => undefined);
          return;
        }
        let off = 0;
        while (off < chunk.length) {
          while (st.sendWindow <= 0) {
            if (st.reset || st.released || st.visitorGone || s.closed) return;
            await st.waitWindow();
          }
          if (st.reset || st.released || st.visitorGone) return;
          const n = Math.min(lt.MAX_DATA, chunk.length - off, st.sendWindow);
          st.sendWindow -= n;
          this.send(lt.dataFrame(st.id, chunk.subarray(off, off + n)));
          off += n;
        }
      }
    }
    if (st.reset || st.released || st.visitorGone) return;
    this.send(lt.endFrame(st.id));
    st.requestEnded = true;
  }
}
