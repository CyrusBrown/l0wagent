import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'

const Q = 'client_id=c1&redirect_uri=https%3A%2F%2Fclaude.ai%2Fcb&state=xyz&code_challenge=abc&code_challenge_method=S256&scope=read+write&response_type=code&resource=r'
let posted: { url: string; body: unknown; client?: string | null }[] = []
let requestResponse: () => Response

beforeEach(() => {
  posted = []
  requestResponse = () => Response.json({
    client_name: 'Claude Desktop', redirect_host: 'claude.ai',
    scopes: [{ scope: 'read', description: 'Read your threads' }, { scope: 'write', description: 'Propose cards' }],
  })
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const req = input as Request
    const url = new URL(req.url, 'http://localhost')
    if (url.pathname === '/api/meta') return Response.json({ setup_complete: true })
    if (url.pathname === '/api/auth/me') return Response.json({ username: 'o' })
    if (url.pathname === '/api/connector/request') return requestResponse()
    if (url.pathname === '/api/connector/consent') {
      posted.push({ url: url.pathname, body: await req.json(), client: req.headers.get('X-L0W-Client') })
      return Response.json({ redirect: 'https://claude.ai/cb?code=1' })
    }
    return new Response('{"error":"not_found"}', { status: 404 })
  }))
})
afterEach(() => { vi.unstubAllGlobals() })

function renderConnect() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [`/connect?${Q}`] })} /></QueryClientProvider>)
}

describe('connect consent', () => {
  it('shows the client and scopes', async () => {
    renderConnect()
    expect(await screen.findByText(/Claude Desktop wants to connect/)).toBeInTheDocument()
    expect(screen.getByText(/return to claude.ai/)).toBeInTheDocument()
    expect(screen.getByText('Propose cards')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'read' })).toBeDisabled()
  })

  it('Allow posts the chosen scopes and follows the redirect', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, origin: 'http://localhost', assign })
    renderConnect()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'write' }))
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://claude.ai/cb?code=1'))
    expect(posted[0].client).toBe('web')
    expect(posted[0].body).toEqual({
      params: Object.fromEntries(new URLSearchParams(Q)), approve: true, scopes: ['read'],
    })
  })

  it('shows an error for an invalid request', async () => {
    requestResponse = () => Response.json({ error: 'invalid_request', detail: 'unknown client' }, { status: 400 })
    renderConnect()
    expect(await screen.findByText(/This connection request isn't valid: unknown client/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull()
  })
})
