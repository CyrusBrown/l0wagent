// `l0w-invite:<hub base URL>#<code>` (membership.md).
import { InviteError } from "./errors";

export const PREFIX = "l0w-invite:";
const CODE = /^[A-Za-z0-9_.~-]+$/;

export interface Invite {
  baseUrl: string; // scheme://host[:port], no trailing slash
  code: string;
}

/** `allowInsecure` permits http:// (local development only; the protocol is HTTPS). */
export function parseInvite(s: unknown, allowInsecure = false): Invite {
  if (typeof s !== "string" || !s.startsWith(PREFIX)) throw new InviteError("not an l0w-invite string");
  const rest = s.slice(PREFIX.length);
  const i = rest.indexOf("#");
  const url = i < 0 ? "" : rest.slice(0, i);
  const code = i < 0 ? "" : rest.slice(i + 1);
  if (i < 0 || !code || code.includes("#")) throw new InviteError("invite has no code");
  if (!CODE.test(code)) throw new InviteError("invite code has invalid characters");
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    throw new InviteError("bad hub URL");
  }
  const okScheme = u.protocol === "https:" || (allowInsecure && u.protocol === "http:");
  if (!okScheme || !u.hostname) throw new InviteError("hub URL must be https");
  if ((u.pathname !== "" && u.pathname !== "/") || u.search || url.includes("?") || u.hash || u.username || u.password) {
    throw new InviteError("hub URL must be a base URL (scheme and host only)");
  }
  return { baseUrl: `${u.protocol}//${u.host.toLowerCase()}`, code };
}

export function formatInvite(baseUrl: string, code: string): string {
  return `${PREFIX}${baseUrl.replace(/\/+$/, "")}#${code}`;
}
