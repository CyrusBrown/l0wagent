// Tunnel framing and handshake messages (tunnel.md).
import * as b64 from "./b64";
import { FrameError } from "./errors";

export const SUBPROTOCOL = "l0w-tunnel.0.1";
export const MAX_DATA = 65536;
export const INITIAL_WINDOW = 262144;
export const HEADER_LEN = 6;
export const PONG_FLAG = 0x01;

export const OPEN = 0x01;
export const RESPONSE = 0x02;
export const DATA = 0x03;
export const END = 0x04;
export const RESET = 0x05;
export const CREDIT = 0x06;
export const PING = 0x07;
const NAMES: Record<number, string> = { 1: "OPEN", 2: "RESPONSE", 3: "DATA", 4: "END", 5: "RESET", 6: "CREDIT", 7: "PING" };

// WebSocket close codes
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_REPLACED = 4409;
export const CLOSE_UPGRADE_REQUIRED = 4426;

export type Headers2 = [string, string][];

export interface Frame {
  type: number;
  streamId: number;
  payload: Uint8Array;
  flags: number;
}

export const frame = (type: number, streamId: number, payload: Uint8Array = new Uint8Array(0), flags = 0): Frame => ({
  type,
  streamId,
  payload,
  flags,
});

export const isPong = (f: Frame): boolean => f.type === PING && (f.flags & PONG_FLAG) !== 0;

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

/** The payload of an OPEN, RESPONSE or RESET frame, validated. */
export function frameJson(f: Frame): Json {
  return jsonPayload(f.type, f.payload);
}

function jsonPayload(type: number, payload: Uint8Array): Json {
  let obj: unknown;
  try {
    obj = JSON.parse(b64.fromUtf8(payload));
  } catch {
    throw new FrameError(`${NAMES[type]} payload is not JSON`);
  }
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) throw new FrameError(`${NAMES[type]} payload must be an object`);
  const o = obj as Json;
  if (type === OPEN) {
    for (const f of ["method", "target", "host", "client", "scheme"]) {
      if (typeof o[f] !== "string") throw new FrameError(`OPEN needs string '${f}'`);
    }
    checkHeaders(o.headers);
  } else if (type === RESPONSE) {
    if (!Number.isInteger(o.status) || o.status < 100 || o.status > 599) throw new FrameError("RESPONSE needs an integer status");
    checkHeaders(o.headers);
  } else if (type === RESET && typeof o.error !== "string") {
    throw new FrameError("RESET needs a string 'error'");
  }
  return o;
}

function checkHeaders(h: unknown): void {
  if (
    !Array.isArray(h) ||
    !h.every((p) => Array.isArray(p) && p.length === 2 && typeof p[0] === "string" && typeof p[1] === "string")
  ) {
    throw new FrameError("headers must be a list of [name, value] pairs");
  }
  if ((h as string[][]).some((p) => p[0] !== p[0].toLowerCase())) throw new FrameError("header names must be lowercase");
}

export function encode(f: Frame): Uint8Array {
  if (!(f.type in NAMES)) throw new FrameError(`unknown frame type 0x${f.type.toString(16)}`);
  if (f.streamId < 0 || f.streamId > 0xffffffff || f.flags < 0 || f.flags > 0xff) throw new FrameError("stream id or flags out of range");
  validate(f);
  const out = new Uint8Array(HEADER_LEN + f.payload.length);
  const dv = new DataView(out.buffer);
  dv.setUint8(0, f.type);
  dv.setUint8(1, f.flags);
  dv.setUint32(2, f.streamId);
  out.set(f.payload, HEADER_LEN);
  return out;
}

/** Parse one binary WebSocket message. Unknown flags are ignored; unknown types are rejected. */
export function decode(data: Uint8Array): Frame {
  if (data.length < HEADER_LEN) throw new FrameError("frame shorter than its header");
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const type = dv.getUint8(0);
  if (!(type in NAMES)) throw new FrameError(`unknown frame type 0x${type.toString(16)}`);
  const f = frame(type, dv.getUint32(2), data.subarray(HEADER_LEN), dv.getUint8(1));
  validate(f);
  return f;
}

function validate(f: Frame): void {
  const t = f.type;
  const n = f.payload.length;
  if (t === PING) {
    if (f.streamId !== 0) throw new FrameError("PING/PONG uses stream id 0");
    if (n !== 8) throw new FrameError("PING/PONG payload must be 8 bytes");
    return;
  }
  if (f.streamId === 0) throw new FrameError(`${NAMES[t]} needs a nonzero stream id`);
  if (t === DATA && n > MAX_DATA) throw new FrameError("DATA frame over 65536 bytes");
  else if (t === END && n) throw new FrameError("END has no payload");
  else if (t === CREDIT && n !== 4) throw new FrameError("CREDIT payload must be 4 bytes");
  else if (t === OPEN || t === RESPONSE || t === RESET) jsonPayload(t, f.payload);
}

const j = (o: unknown): Uint8Array => b64.utf8(JSON.stringify(o));

export function openFrame(streamId: number, method: string, target: string, host: string, headers: Headers2, client: string, scheme = "https"): Frame {
  if (streamId % 2 === 0) throw new FrameError("only the hub opens streams, with odd ids");
  return frame(OPEN, streamId, j({ method, target, host, headers, client, scheme }));
}
export const responseFrame = (streamId: number, status: number, headers: Headers2): Frame => frame(RESPONSE, streamId, j({ status, headers }));
export const dataFrame = (streamId: number, body: Uint8Array): Frame => frame(DATA, streamId, body);
export const endFrame = (streamId: number): Frame => frame(END, streamId);
export const resetFrame = (streamId: number, error: string): Frame => frame(RESET, streamId, j({ error }));
export function creditFrame(streamId: number, n: number): Frame {
  if (!Number.isInteger(n) || n < 0 || n > 0xffffffff) throw new FrameError("credit out of range");
  const p = new Uint8Array(4);
  new DataView(p.buffer).setUint32(0, n);
  return frame(CREDIT, streamId, p);
}
export function creditAmount(f: Frame): number {
  if (f.type !== CREDIT || f.payload.length !== 4) throw new FrameError("not a CREDIT frame");
  return new DataView(f.payload.buffer, f.payload.byteOffset, 4).getUint32(0);
}
export const pingFrame = (payload: Uint8Array): Frame => frame(PING, 0, payload);
export function pongFor(ping: Frame): Frame {
  if (ping.type !== PING || isPong(ping)) throw new FrameError("not a PING");
  return frame(PING, 0, ping.payload, PONG_FLAG);
}

// --- text messages of the handshake -------------------------------------------------------------

export function challengeMessage(nonce: Uint8Array, hub: string): string {
  if (nonce.length !== 32) throw new FrameError("nonce must be 32 bytes");
  return JSON.stringify({ t: "challenge", nonce: b64.encode(nonce), hub });
}

export function readyMessage(session: string, limits: Record<string, unknown>): string {
  return JSON.stringify({ t: "ready", session, limits });
}

export const DROP_HEADERS: ReadonlySet<string> = new Set(["connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"]);

/** What the hub MUST do before OPEN: lowercase names; drop hop-by-hop, proxy-* and x-l0w-tunnel-*. */
export function filterRequestHeaders(headers: Iterable<[string, string]>): Headers2 {
  const out: Headers2 = [];
  for (const [k0, v] of headers) {
    const k = k0.toLowerCase();
    if (DROP_HEADERS.has(k) || k.startsWith("proxy-") || k.startsWith("x-l0w-tunnel-")) continue;
    out.push([k, v]);
  }
  return out;
}
