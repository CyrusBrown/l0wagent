import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
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


function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

const base = { available: true, state: 'standalone' }
const hub = { name: 'Example Hub', domain: 'hub.example', base_url: 'https://hub.example', policies: { terms: 'https://hub.example/terms' } }

describe('Settings → Hub', () => {
  it('standalone shows the invite field', async () => {
    overrides['/api/membership'] = base
    renderAt('/settings/hub')
    expect(await screen.findByLabelText('Invite')).toBeTruthy()
    expect(screen.getByText('Read invite')).toBeTruthy()
    expect(screen.queryByText('Join')).toBeNull()
  })

  it('a preview with problems shows them and disables Join', async () => {
    overrides['/api/membership'] = base
    overrides['/api/membership/preview'] = {
      hub, name_hint: 'fern', expires: null, own_url_allowed: false, two_factor_ready: false,
      example: { instance: 'fern.hub.example', files: 'fern-files.hub.example' }, problems: ['Two-step sign-in is not on'],
    }
    renderAt('/settings/hub')
    fireEvent.change(await screen.findByLabelText('Invite'), { target: { value: 'l0w-invite:abc' } })
    fireEvent.click(screen.getByText('Read invite'))
    expect(await screen.findByText('Two-step sign-in is not on')).toBeTruthy()
    expect(screen.getByText('Example Hub')).toBeTruthy()
    fireEvent.click(screen.getByLabelText(/I've made a backup/))
    expect((screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('joined shows the address and the tunnel state', async () => {
    overrides['/api/membership'] = {
      ...base, state: 'joined', address: 'fern.hub.example', mode: 'tunnel', hub,
      urls: { instance: 'https://fern.hub.example', files: 'https://fern-files.hub.example' },
      tunnel: { state: 'connected', since: '2026-11-18T10:00:00Z', error: null },
      check: { ok: true, at: '2026-11-18T10:05:00Z', problems: [] },
      sign_key: { kid: 'abcdef0123456789', created: '2026-11-18T09:00:00Z' },
    }
    renderAt('/settings/hub')
    expect((await screen.findAllByText('fern.hub.example')).length).toBeGreaterThan(0)
    expect(screen.getByText(/connected/)).toBeTruthy()
    expect(screen.getByText('Check now')).toBeTruthy()
  })
})
