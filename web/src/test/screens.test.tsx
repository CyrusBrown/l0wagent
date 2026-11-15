import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

// Render each ported screen against snapshots of the mock server's responses
// (regenerate with control/scripts/dump_mock_responses.py).
const table = responses as Record<string, unknown>

let overrides: Record<string, unknown> = {}

function respond(url: URL): Response {
  if (url.pathname in overrides) {
    const o = overrides[url.pathname]
    return o instanceof Response ? o : Response.json(o)
  }
  if (url.pathname === '/api/events') return new Response(new ReadableStream()) // stays open, like the real stream
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

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

describe('screens render with mock data', () => {
  it('Home', async () => {
    renderAt('/')
    expect(await screen.findByText(/Waiting on you/)).toBeInTheDocument()
    expect(await screen.findByText('Budget this week')).toBeInTheDocument()
    expect(await screen.findByText('Re-index photo library')).toBeInTheDocument()
  })
  it('Thread with cards', async () => {
    renderAt('/t/1')
    expect(await screen.findByRole('heading', { name: 'Pump controller firmware' })).toBeInTheDocument()
    expect(await screen.findByText('Question · pick one')).toBeInTheDocument()
    expect(screen.getByText('Promote request')).toBeInTheDocument()
    expect(screen.getByText('Allow for project')).toBeInTheDocument()
  })
  it('Inbox', async () => {
    renderAt('/inbox')
    expect(await screen.findByText(/waiting on you/)).toBeInTheDocument()
    expect(await screen.findByText(/Review the 3 unreadable receipts/)).toBeInTheDocument()
  })
  it('Runs and a run', async () => {
    renderAt('/runs')
    expect(await screen.findByText('Weekly review across all projects')).toBeInTheDocument()
  })
  it('Run detail', async () => {
    renderAt('/runs/123')
    expect(await screen.findByText('Status timeline')).toBeInTheDocument()
    expect(screen.getByText(/Reproduced the double trigger/)).toBeInTheDocument()
  })
  it('Files', async () => {
    renderAt('/files')
    expect(await screen.findByText('NOW.md')).toBeInTheDocument()
  })
  it('Project', async () => {
    renderAt('/p/garden-irrigation')
    expect(await screen.findByRole('heading', { name: 'Garden irrigation' })).toBeInTheDocument()
  })
  it('Settings: account', async () => {
    renderAt('/settings/account')
    expect(await screen.findByText('Sessions and devices')).toBeInTheDocument()
    expect(screen.getByText('Two-factor authentication')).toBeInTheDocument()
    expect(await screen.findByText('This device')).toBeInTheDocument()
  })
  it('Settings: system and the self-test', async () => {
    renderAt('/settings/system')
    expect(await screen.findByText('Runs can start')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Run self-test' }))
    expect(await screen.findByText("Can't reach the LAN or the host")).toBeInTheDocument()
  })
  it('Settings: a later tab says when it arrives', async () => {
    renderAt('/settings/budgets')
    expect(await screen.findByText(/arrive with M4/)).toBeInTheDocument()
  })
  it('Login when signed out', async () => {
    overrides = { '/api/auth/me': new Response('{"error":"unauthorized"}', { status: 401 }) }
    renderAt('/')
    expect(await screen.findByText('Sign in to your assistant')).toBeInTheDocument()
  })
  it('Setup wizard on a fresh server', async () => {
    const meta = (responses as unknown as Record<string, Record<string, unknown>>)['/api/meta']
    overrides = { '/api/meta': { ...meta, setup_complete: false }, '/api/auth/me': new Response('{"error":"unauthorized"}', { status: 401 }) }
    renderAt('/')
    expect(await screen.findByRole('heading', { name: 'Create the owner' })).toBeInTheDocument()
    expect(screen.getByText('Self-test')).toBeInTheDocument()
  })
  it('Tools & Secrets', async () => {
    renderAt('/tools')
    expect(await screen.findByText('GITHUB_TOKEN')).toBeInTheDocument()
    expect(screen.getByText(/Values are write-only/)).toBeInTheDocument()
  })
  it('New', async () => {
    renderAt('/new')
    expect(await screen.findByPlaceholderText('What should the assistant work on?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '1:1 chat' })).toBeDisabled()
  })
  it('Project tabs', async () => {
    renderAt('/p/garden-irrigation')
    fireEvent.click(await screen.findByRole('button', { name: /^Asks/ }))
    expect((await screen.findAllByText(/Should I flash the fix|Write access/)).length).toBeGreaterThan(0)
  })
})
