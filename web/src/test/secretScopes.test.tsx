import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
const puts: { url: string; body: unknown }[] = []

beforeEach(() => {
  localStorage.clear()
  puts.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : null
    const url = new URL(req ? req.url : String(input), 'http://localhost')
    const method = req?.method ?? init?.method ?? 'GET'
    if (method === 'PUT') {
      puts.push({ url: url.pathname, body: JSON.parse(await (req ? req.text() : String(init?.body))) })
      return Response.json({})
    }
    if (url.pathname === '/api/events') return new Response(new ReadableStream())
    const exact = table[url.pathname + url.search]
    const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
    const body = exact ?? loose
    return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => vi.unstubAllGlobals())

function renderTools() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/tools'] })} /></QueryClientProvider>)
}

describe('secret scopes', () => {
  it('shows each secret\'s scope label', async () => {
    renderTools()
    expect(await screen.findByText('GITHUB_TOKEN')).toBeInTheDocument()
    expect(await screen.findByText('Garden irrigation')).toBeInTheDocument()
    expect(screen.getAllByText('All projects').length).toBeGreaterThan(0)
  })

  it('changing a scope sends only the scope', async () => {
    renderTools()
    const name = await screen.findByText('GITHUB_TOKEN')
    const row = name.closest('.secrets-row') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: /Scope of GITHUB_TOKEN/ }))
    fireEvent.click(await screen.findByRole('option', { name: /Integrations only/ }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0].url).toBe('/api/secrets/GITHUB_TOKEN')
    expect(puts[0].body).toEqual({ scope: 'integrations' })
  })
})
