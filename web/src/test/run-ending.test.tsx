import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
let overrides: Record<string, unknown> = {}

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
    if (url.pathname in overrides) return Response.json(overrides[url.pathname])
    if (url.pathname === '/api/events') return new Response(new ReadableStream())
    const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
    return loose === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(loose)
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => { vi.unstubAllGlobals(); overrides = {} })

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

function withRun(run: object) {
  const d = table['/api/runs/123'] as { run: object }
  overrides['/api/runs/123'] = { ...d, run: { ...d.run, ...run } }
}

describe('how a run ended', () => {
  it('a stopped run says why in plain words, and it is not an alert', async () => {
    withRun({ status: 'stopped', ending: 'Stopped: 5-hour usage reached your reserve (70%).' })
    renderAt('/runs/123')
    const line = await screen.findByText('Stopped: 5-hour usage reached your reserve (70%).')
    expect(line.getAttribute('role')).toBeNull()
    expect(line.className).not.toContain('bad')
  })

  it('a failed run shows its error as an alert', async () => {
    withRun({ status: 'failed', ending: "Claude's login expired: run ./l0w claude-login on the server." })
    renderAt('/runs/123')
    const line = await screen.findByText("Claude's login expired: run ./l0w claude-login on the server.")
    expect(line.getAttribute('role')).toBe('alert')
    expect(line.className).toContain('bad')
  })

  it('a running run shows no ending', async () => {
    withRun({ ending: 'Finished.' })
    renderAt('/runs/123')
    await screen.findByText('Run #123')
    expect(screen.queryByText('Finished.')).toBeNull()
  })
})
