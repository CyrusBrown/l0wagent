import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
let calls: { method: string; path: string; body: unknown }[] = []

beforeEach(() => {
  localStorage.clear()
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : null
    const url = new URL(req ? req.url : String(input), 'http://localhost')
    const method = (req?.method ?? init?.method ?? 'GET').toUpperCase()
    if (url.pathname === '/api/events') return new Response(new ReadableStream())
    if (method !== 'GET') {
      const text = req ? await req.clone().text() : String(init?.body ?? '')
      calls.push({ method, path: url.pathname, body: text ? JSON.parse(text) : null })
      return Response.json({})
    }
    const body = table[url.pathname + url.search] ?? Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
    return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => vi.unstubAllGlobals())

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

describe('Tools & Secrets: integrations and connected apps', () => {
  it('shows integrations with their tools and saves a policy change', async () => {
    renderAt('/tools?tab=integrations')
    expect(await screen.findByText('Fastmail')).toBeInTheDocument()
    const group = await screen.findByRole('radiogroup', { name: 'Policy for send_message' })
    const current = within(group).getAllByRole('radio').find((b) => b.getAttribute('aria-checked') === 'true')!
    const target = within(group).getAllByRole('radio').find((b) => b !== current)!
    fireEvent.click(target)
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.path).toBe('/api/integrations/fastmail/tools/send_message')
    expect(put.body).toEqual({ policy: target.textContent!.toLowerCase() })
  })

  it('lists connected apps and revokes after confirming', async () => {
    renderAt('/tools?tab=apps')
    expect(await screen.findByText('Claude Desktop')).toBeInTheDocument()
    expect(screen.getByText(/Read · Write/)).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Revoke' })[0])
    expect(calls).toHaveLength(0)
    fireEvent.click(await screen.findByRole('button', { name: /^Revoke Claude/ }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    expect(calls.find((c) => c.method === 'DELETE')!.path).toBe('/api/connected-apps/2')
  })
})
