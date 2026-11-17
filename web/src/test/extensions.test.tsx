import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import { ExtElement } from '../ext/ExtElement'
import { createHost } from '../ext/host'
import { publishExtensionEvent } from '../ext/bus'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
let overrides: Record<string, unknown> = {}
const calls: { url: string; method: string; init?: RequestInit }[] = []

function respond(url: URL): Response {
  if (url.pathname in overrides) {
    const o = overrides[url.pathname]
    return o instanceof Response ? o : Response.json(o)
  }
  if (url.pathname === '/api/events') return new Response(new ReadableStream())
  const exact = table[url.pathname + url.search]
  const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
  const body = exact ?? loose
  return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
}

beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = input instanceof Request ? input.url : String(input)
    calls.push({ url: raw, method: input instanceof Request ? input.method : init?.method ?? 'GET', init })
    return respond(new URL(raw, 'http://localhost'))
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => { vi.unstubAllGlobals(); overrides = {} })

describe('the extension host', () => {
  it('api() calls the extension route with the client header and a JSON body', async () => {
    overrides['/api/ext/plantlog/plants'] = [{ name: 'fern' }]
    const host = createHost('plantlog', () => {})
    expect(await host.api('/plants', { method: 'POST', body: { a: 1 } })).toEqual([{ name: 'fern' }])
    const c = calls[0]
    expect(c.url).toBe('/api/ext/plantlog/plants')
    expect(c.init?.method).toBe('POST')
    expect(c.init?.credentials).toBe('include')
    expect(c.init?.headers).toMatchObject({ 'X-L0W-Client': 'web', 'Content-Type': 'application/json' })
    expect(c.init?.body).toBe('{"a":1}')
  })
  it('api() throws the response detail with the status on a non-2xx', async () => {
    overrides['/api/ext/plantlog/bad'] = new Response(JSON.stringify({ detail: 'No such plant' }), { status: 404, statusText: 'Not Found' })
    const host = createHost('plantlog', () => {})
    await expect(host.api('/bad')).rejects.toMatchObject({ message: 'No such plant', status: 404 })
  })
  it('on() receives only that extension\'s events of that type, and off() stops them', () => {
    const host = createHost('plantlog', () => {})
    const fn = vi.fn()
    const off = host.on('plants_changed', fn)
    publishExtensionEvent('plantlog', 'plants_changed', { n: 1 })
    publishExtensionEvent('other', 'plants_changed', { n: 2 })
    publishExtensionEvent('plantlog', 'else', { n: 3 })
    off()
    publishExtensionEvent('plantlog', 'plants_changed', { n: 4 })
    expect(fn.mock.calls).toEqual([[{ n: 1 }]])
  })
})

describe('ExtElement', () => {
  it('sets l0w and context before the element is connected, and again when the context changes', () => {
    const seen: { l0w: unknown; context: unknown }[] = []
    class T extends HTMLElement {
      l0w?: unknown
      context?: unknown
      connectedCallback() { seen.push({ l0w: this.l0w, context: this.context }) }
    }
    customElements.define('test-ext-el', T)
    const host = createHost('t', () => {})
    const a = { project: 'x' }
    const { rerender, container, unmount } = render(<ExtElement element="test-ext-el" l0w={host} context={a} />)
    expect(seen).toHaveLength(1)
    expect(seen[0].l0w).toBe(host)
    expect(seen[0].context).toBe(a)
    const el = container.querySelector('test-ext-el') as T
    const b = { project: 'y' }
    rerender(<ExtElement element="test-ext-el" l0w={host} context={b} />)
    expect(el.context).toBe(b)
    unmount()
    expect(container.querySelector('test-ext-el')).toBeNull()
  })
})

const setting = (o: Record<string, unknown>) => ({ label: null, help: null, min: null, max: null, choices: null, ...o }) as unknown as Record<string, unknown> & { key: string }
const plantlog = {
  name: 'plantlog', title: 'Plant log', version: '0.2.0', sdk: '1', source: 'local', description: 'Tracks watering.',
  enabled: true, loaded: false, present: true, compatible: true, last_error: 'Traceback:\n  boom', folders: [], prompt_slots: [], routines: [], ui: null,
  commands: [{ name: 'water', help: 'Mark a plant watered', usage: 'assistant plantlog water <plant>' }],
  settings: [
    setting({ key: 'remind', kind: 'bool', label: 'Remind me', default: false, value: false }),
    setting({ key: 'days', kind: 'int', label: 'Days between waterings', default: 7, value: 7, min: 1, max: 30 }),
    setting({ key: 'mode', kind: 'choice', label: 'Mode', default: 'a', value: 'a', choices: ['a', 'b'] }),
  ],
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

describe('Settings → Extensions', () => {
  it('shows an extension with its error, settings form and commands; settings save on change', async () => {
    overrides['/api/extensions'] = [plantlog]
    overrides['/api/extensions/plantlog/settings'] = { ...plantlog, settings: plantlog.settings.map((s) => (s.key === 'remind' ? { ...s, value: true } : s)) }
    renderAt('/settings/extensions')
    expect(await screen.findByText('Plant log')).toBeInTheDocument()
    expect(screen.getByText('Tracks watering.')).toBeInTheDocument()
    expect(screen.getByText(/boom/)).toBeInTheDocument()
    expect(screen.getByText('Days between waterings')).toBeInTheDocument()
    expect(screen.getByText('assistant plantlog water <plant>')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Remind me' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/api/extensions/plantlog/settings') && c.method === 'PUT')).toBe(true))
  })
  it('shows the reason when enabling fails', async () => {
    overrides['/api/extensions'] = [{ ...plantlog, enabled: false, last_error: null, settings: [], commands: [] }]
    overrides['/api/extensions/plantlog/enabled'] = new Response(JSON.stringify({ detail: 'It would not start' }), { status: 409 })
    renderAt('/settings/extensions')
    fireEvent.click(await screen.findByRole('switch', { name: 'Plant log enabled' }))
    expect(await screen.findByText('It would not start')).toBeInTheDocument()
  })
})
