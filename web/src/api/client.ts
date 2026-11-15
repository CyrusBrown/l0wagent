import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

export type S = components['schemas']

/** Typed API client. Paths and bodies are checked against control/openapi.json. */
export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? '',
  credentials: 'include',
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
export const newKey = () => crypto.randomUUID()
