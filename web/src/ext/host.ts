import { CLIENT_HEADERS } from '../api/client'
import { subscribeExtensionEvent } from './bus'

/** What an extension's element gets as `el.l0w`. */
export interface L0wHost {
  ext: string
  api: (path: string, opts?: { method?: string; body?: unknown }) => Promise<any> // eslint-disable-line @typescript-eslint/no-explicit-any
  on: (type: string, fn: (data: any) => void) => () => void // eslint-disable-line @typescript-eslint/no-explicit-any
  navigate: (to: string) => void
  readonly theme: 'light' | 'dark'
}

export class ExtApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

/** The object handed to an extension's elements. Its api() calls only that extension's own routes. */
export function createHost(ext: string, navigate: (to: string) => void): L0wHost {
  return {
    ext,
    async api(path, { method = 'GET', body } = {}) {
      const headers: Record<string, string> = { ...CLIENT_HEADERS }
      if (body !== undefined) headers['Content-Type'] = 'application/json'
      const res = await fetch(`/api/ext/${ext}${path}`, {
        method, credentials: 'include', headers, body: body !== undefined ? JSON.stringify(body) : undefined,
      })
      if (!res.ok) {
        const j = await res.json().catch(() => null)
        const detail = j && typeof j === 'object' && typeof j.detail === 'string' ? j.detail : null
        throw new ExtApiError(detail || res.statusText || `HTTP ${res.status}`, res.status)
      }
      if (res.status === 204) return null
      const text = await res.text()
      return text ? JSON.parse(text) : null
    },
    on: (type, fn) => subscribeExtensionEvent(ext, type, fn),
    navigate,
    get theme() { return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light' },
  }
}
