import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

describe('run budget', () => {
  it('+10 pts on a running run asks control to add 10 points', async () => {
    overrides['/api/runs/123/budget'] = { ...(table['/api/runs/123'] as { run: object }).run, budget: 20 }
    renderAt('/runs/123')
    fireEvent.click(await screen.findByRole('button', { name: '+10 pts' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/api/runs/123/budget'))).toBe(true))
    const call = calls.find((c) => c.method === 'POST' && c.url.endsWith('/api/runs/123/budget'))!
    const sent = call.init?.body ?? (await (vi.mocked(fetch).mock.calls.find(([i]) => i instanceof Request && i.url.endsWith('/budget'))![0] as Request).clone().text())
    expect(JSON.parse(String(sent))).toEqual({ add: 10 })
  })

  it('a finished run has no +10 pts button', async () => {
    const d = table['/api/runs/123'] as { run: object }
    overrides['/api/runs/123'] = { ...d, run: { ...d.run, status: 'done' } }
    renderAt('/runs/123')
    await screen.findByText('Run #123')
    expect(screen.queryByRole('button', { name: '+10 pts' })).toBeNull()
  })
})
