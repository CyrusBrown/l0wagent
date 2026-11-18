// RFC 3339 UTC times with a Z and whole seconds (README rules).
import { MAX_SKEW } from "./consts";
import { ValidationError } from "./errors";

const RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;

export function formatTime(epoch: number): string {
  return new Date(Math.floor(epoch) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function parseTime(s: unknown): number {
  if (typeof s !== "string" || !RE.test(s)) throw new ValidationError(`bad time ${JSON.stringify(s)}: want YYYY-MM-DDTHH:MM:SSZ`);
  const ms = Date.parse(s);
  // Date.parse accepts some impossible dates by rolling them over; refuse those like strptime does.
  if (Number.isNaN(ms) || formatTime(ms / 1000) !== s || Number(s.slice(0, 4)) < 1) {
    throw new ValidationError(`bad time ${JSON.stringify(s)}`);
  }
  return ms / 1000;
}

export const now = (): number => Math.floor(Date.now() / 1000);

/** `iat` within `skew` seconds of now, either direction (registration, tunnel-hello). */
export function checkFresh(iat: unknown, nowEpoch?: number, skew = MAX_SKEW): void {
  const t = parseTime(iat);
  const n = nowEpoch ?? now();
  if (Math.abs(n - t) > skew) throw new ValidationError("iat outside allowed clock skew");
}
