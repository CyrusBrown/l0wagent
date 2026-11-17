import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
const calls: { url: string; method: string; body?: string }[] = []
let advanced = [
  { key: 'REPLY_WAIT_MIN', group: 'Conversation', label: 'Wait for your reply', help: 'How long it waits.', kind: 'int', value: 5, default: 5, min: 1, max: 60, unit: 'min' },
  { key: 'FLAG', group: 'Runs', label: 'A flag', help: 'Flag help.', kind: 'bool', value: true, default: false, min: null, max: null, unit: null },
]

beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = input instanceof Request ? input.url : String(input)
    const method = input instanceof Request ? input.method : init?.method ?? 'GET'
    const url = new URL(raw, 'http://localhost')
    const body = input instanceof Request ? await input.clone().text() : (init?.body as string | undefined)
    calls.push({ url: url.pathname, method, body })
    if (url.pathname === '/api/settings/advanced') {
      if (method === 'PATCH') {
        const v = JSON.parse(body!).values as Record<string, unknown>
        advanced = advanced.map((s) => (s.key in v ? { ...s, value: v[s.key] as never } : s))
      }
      return Response.json(advanced)
    }
    if (url.pathname === '/api/events') return new Response(new ReadableStream())
    const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
    return loose === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(loose)
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => vi.unstubAllGlobals())

describe('Settings → Advanced', () => {
  it('shows the settings by group and saves a change on blur', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/settings/advanced'] })} /></QueryClientProvider>)
    expect(await screen.findByText('Fine-tuning. The 1:1 chat can change these for you too.')).toBeInTheDocument()
    await waitFor(() => expect([...document.querySelectorAll('.set-title')].map((e) => e.textContent)).toEqual(['Advanced', 'Conversation', 'Runs']))
    expect(screen.getByText('min')).toBeInTheDocument()
    expect(screen.getByText(/Default false/)).toBeInTheDocument()
    const input = screen.getByLabelText('Wait for your reply')
    fireEvent.change(input, { target: { value: '7' } })
    fireEvent.blur(input)
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH' && c.body === '{"values":{"REPLY_WAIT_MIN":7}}')).toBe(true))
    await waitFor(() => expect(screen.getByText(/Default 5 min/)).toBeInTheDocument())
  })
})
