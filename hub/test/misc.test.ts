import { describe, expect, it } from "vitest";
import { AsyncQueue, SlidingWindow } from "../src/queue";
import { offlineResponse, prefersJson } from "../src/respond";
import * as lt from "../src/proto/tunnel";
import * as b64 from "../src/proto/b64";
import { formatTime, parseTime, checkFresh } from "../src/proto/times";
import { formatInvite } from "../src/proto/invite";

describe("prefersJson", () => {
  it("follows the Accept header", () => {
    expect(prefersJson("application/json")).toBe(true);
    expect(prefersJson("text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")).toBe(false);
    expect(prefersJson("")).toBe(false);
    expect(prefersJson("*/*")).toBe(false);
    expect(prefersJson("application/json;q=0.5, text/html")).toBe(false);
    expect(prefersJson("text/html;q=0.5, application/json")).toBe(true);
  });
  it("the offline answer is a 502 with Retry-After and never cached", async () => {
    const j = offlineResponse("application/json");
    expect([j.status, j.headers.get("retry-after"), j.headers.get("cache-control")]).toEqual([502, "30", "no-store"]);
    expect(await j.json()).toEqual({ v: "l0w/0.1", error: "instance_offline" });
    const h = offlineResponse("text/html");
    expect(h.status).toBe(502);
    expect(h.headers.get("content-type")).toMatch(/^text\/html/);
  });
});

describe("SlidingWindow", () => {
  it("allows `limit` hits per minute and forgets old ones", () => {
    const w = new SlidingWindow();
    for (let i = 0; i < 3; i++) expect(w.allow(3, 1000 + i)).toBe(true);
    expect(w.allow(3, 2000)).toBe(false);
    expect(w.hits.length).toBe(3);
    expect(w.allow(3, 61_001)).toBe(true); // the first three are older than a minute
  });
});

describe("AsyncQueue", () => {
  it("hands items to waiters in order", async () => {
    const q = new AsyncQueue<number>();
    const a = q.shift();
    q.push(1);
    q.push(2);
    expect(await a).toBe(1);
    expect(await q.shift()).toBe(2);
  });
});

describe("tunnel helpers", () => {
  it("filters request headers", () => {
    const out = lt.filterRequestHeaders([
      ["Connection", "keep-alive"],
      ["X-L0W-Tunnel-Client", "6.6.6.6"],
      ["Proxy-Authorization", "x"],
      ["TE", "trailers"],
      ["Accept", "text/html"],
    ]);
    expect(out).toEqual([["accept", "text/html"]]);
  });
  it("round-trips frames", () => {
    const o = lt.openFrame(1, "GET", "/x?y=1", "mara.hub.example", [["accept", "*/*"]], "203.0.113.9", "https");
    expect(lt.frameJson(lt.decode(lt.encode(o))).target).toBe("/x?y=1");
    expect(() => lt.openFrame(2, "GET", "/", "h", [], "1.1.1.1")).toThrow();
    expect(lt.creditAmount(lt.decode(lt.encode(lt.creditFrame(3, 262144))))).toBe(262144);
    const ping = lt.pingFrame(new Uint8Array(8));
    expect(lt.isPong(lt.pongFor(ping))).toBe(true);
    expect(() => lt.decode(new Uint8Array([9, 0, 0, 0, 0, 1]))).toThrow();
  });
  it("challenge message has a 32-byte nonce", () => {
    const m = JSON.parse(lt.challengeMessage(new Uint8Array(32), "hub.example"));
    expect(b64.decode(m.nonce).length).toBe(32);
    expect(m.t).toBe("challenge");
  });
});

describe("times and invites", () => {
  it("formats and parses whole-second UTC times", () => {
    expect(formatTime(1794951000)).toBe("2026-11-17T21:30:00Z");
    expect(parseTime("2026-11-17T21:30:00Z")).toBe(1794951000);
    for (const bad of ["2026-11-17 21:30:00Z", "2026-11-17T21:30:00+00:00", "2026-02-30T00:00:00Z", "2026-11-17T21:30:00.5Z", 5, null]) {
      expect(() => parseTime(bad as string)).toThrow();
    }
  });
  it("checkFresh allows 300 s of skew either way", () => {
    const n = 1794951000;
    checkFresh(formatTime(n - 300), n);
    checkFresh(formatTime(n + 300), n);
    expect(() => checkFresh(formatTime(n - 301), n)).toThrow();
    expect(() => checkFresh(formatTime(n + 301), n)).toThrow();
  });
  it("formats an invite", () => {
    expect(formatInvite("https://hub.example/", "abc")).toBe("l0w-invite:https://hub.example#abc");
  });
});
