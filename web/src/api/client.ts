import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

export type S = components['schemas']

/** Typed API client. Paths and bodies are checked against control/openapi.json. */
/** Sent on every request: control refuses cookie-authenticated writes without it (CSRF). */
export const CLIENT_HEADERS = { 'X-L0W-Client': 'web' }

export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? '',
  credentials: 'include',
  headers: CLIENT_HEADERS,
  fetch: (req) => globalThis.fetch(req), // look fetch up per call (lets tests and the desktop app swap it)
})

export class ApiError extends Error {
  constructor(public status: number, public body: unknown) {
    super(typeof body === 'object' && body && 'error' in body ? String((body as { error: unknown }).error) : `HTTP ${status}`)
  }
}

/** Unwrap an openapi-fetch result, throwing ApiError on failure (TanStack Query wants throws). */
export async function ok<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await p
  if (!response.ok) throw new ApiError(response.status, error)
  return data as T
}

/** A client-generated idempotency key for creates (lets the offline outbox retry safely). */
// Idempotency keys. crypto.randomUUID exists only in secure contexts (HTTPS or localhost), and
// people often open the app over plain http on their LAN; getRandomValues works everywhere.
export const newKey = (): string => {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const b = crypto.getRandomValues(new Uint8Array(16))
  b[6] = (b[6] & 0x0f) | 0x40
  b[8] = (b[8] & 0x3f) | 0x80
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}
