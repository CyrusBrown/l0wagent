import type { MemberRoute } from "./member";
import type { TunnelConn } from "./tunnel";

/** Wrangler vars and secrets (README.md has the table). */
export interface Env {
  DB: D1Database;
  MEMBER: DurableObjectNamespace<MemberRoute>;
  CONN: DurableObjectNamespace<TunnelConn>;

  HUB_NAME: string;
  HUB_BASE_URL: string;
  HUB_DOMAIN?: string;
  TUNNEL_URL: string;
  INSTANCE_HOST_PATTERN: string;
  FILES_HOST_PATTERN: string;
  RESERVED_NAMES?: string;
  MIN_RELEASE?: string;
  LIMITS?: string;
  POLICIES?: string;
  OWN_URL?: string;
  INVITE_DAYS?: string;
  HUB_KEY_CREATED?: string;

  // secrets
  HUB_SIGNING_SEED: string;
  ADMIN_TOKEN?: string;
}
