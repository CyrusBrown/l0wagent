import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
const emptyLine = 'No messages yet. Write the first one below.'

let overrides: Record<string, Response | Promise<Response> | unknown> = {}

function respond(url: URL): Response | Promise<Response> {
  if (url.pathname === '/api/events') return new Response(new ReadableStream())
  const override = overrides[url.pathname]
  if (override instanceof Promise) return override
  if (override instanceof Response) return override
  if (override !== undefined) return Response.json(override)
  const exact = table[url.pathname + url.search]
  const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
  const body = exact ?? loose
  return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = input instanceof Request ? input.url : String(input)
    return respond(new URL(raw, 'http://localhost'))
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => { vi.unstubAllGlobals(); overrides = {} })

function renderThread() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/t/1'] })} /></QueryClientProvider>)
}

describe('Thread empty state', () => {
  it('shows a quiet line when the thread has no items', async () => {
    overrides = { '/api/threads/1/items': { items: [] } }
    renderThread()
    expect(await screen.findByText(emptyLine)).toBeInTheDocument()
  })

  it('does not show while items are still loading', async () => {
    overrides = { '/api/threads/1/items': new Promise<Response>(() => {}) }
    renderThread()
    expect(await screen.findByRole('heading', { name: 'Pump controller firmware' })).toBeInTheDocument()
    expect(screen.queryByText(emptyLine)).not.toBeInTheDocument()
  })

  it('does not show when the thread has messages', async () => {
    renderThread()
    expect(await screen.findByText(/Can you check why the pump runs twice/)).toBeInTheDocument()
    expect(screen.queryByText(emptyLine)).not.toBeInTheDocument()
  })
})
