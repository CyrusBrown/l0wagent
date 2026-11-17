import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import responses from './mock-responses.json'

// The phone shell: a bottom tab bar, a More sheet and a conversation drawer. They mount only when
// the viewport matches the phone breakpoint, so the desktop navigation is never duplicated.
const table = responses as Record<string, unknown>

function respond(url: URL): Response {
  if (url.pathname === '/api/events') return new Response(new ReadableStream())
  const exact = table[url.pathname + url.search]
  const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
  const body = exact ?? loose
  return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
}

function mockPhone(on: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: on && q.includes('max-width: 760px'), media: q, addEventListener() {}, removeEventListener() {} }))
}

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = input instanceof Request ? input.url : String(input)
    return respond(new URL(raw, 'http://localhost'))
  }))
})
afterEach(() => { vi.unstubAllGlobals() })

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

describe('phone shell', () => {
  it('has no tab bar on a desktop-width viewport', async () => {
    mockPhone(false)
    renderAt('/')
    await screen.findByText(/Waiting on you/)
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Conversations' })).not.toBeInTheDocument()
  })

  it('shows the tab bar with the current place marked', async () => {
    mockPhone(true)
    renderAt('/runs')
    const bar = await screen.findByRole('navigation', { name: 'Main' })
    const tabs = within(bar)
    expect(tabs.getByRole('link', { name: 'Runs' })).toHaveAttribute('aria-current', 'page')
    expect(tabs.getByRole('link', { name: 'Home' })).not.toHaveAttribute('aria-current')
    expect(tabs.getByRole('button', { name: 'More' })).toBeInTheDocument()
  })

  it('opens the More sheet, lists the rest, and closes with Escape', async () => {
    mockPhone(true)
    renderAt('/runs')
    const more = await screen.findByRole('button', { name: 'More' })
    more.focus()
    fireEvent.click(more)
    const sheet = await screen.findByRole('dialog', { name: 'More' })
    expect(sheet).toHaveAttribute('aria-modal', 'true')
    for (const name of ['Files', 'Tools', 'Calendar', 'Network', 'Web page', 'Settings']) {
      expect(within(sheet).getByRole('link', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    }
    await waitFor(() => expect(sheet).toHaveFocus())
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'More' })).not.toBeInTheDocument())
    expect(more).toHaveFocus()
  })

  it('closes the More sheet when you pick a place', async () => {
    mockPhone(true)
    renderAt('/runs')
    fireEvent.click(await screen.findByRole('button', { name: 'More' }))
    const sheet = await screen.findByRole('dialog', { name: 'More' })
    fireEvent.click(within(sheet).getByRole('link', { name: /^Calendar/ }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'More' })).not.toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: 'Calendar' })).toBeInTheDocument()
  })

  it('reaches the conversation tree through the drawer on a thread', async () => {
    mockPhone(true)
    renderAt('/t/1')
    fireEvent.click(await screen.findByRole('button', { name: 'Conversations' }))
    const drawer = await screen.findByRole('dialog', { name: 'Conversations' })
    expect(drawer).toHaveAttribute('aria-modal', 'true')
    expect(await within(drawer).findByText('General')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Conversations' })).not.toBeInTheDocument())
  })

  it('has no drawer button outside conversation screens', async () => {
    mockPhone(true)
    renderAt('/inbox')
    await screen.findByRole('navigation', { name: 'Main' })
    expect(screen.queryByRole('button', { name: 'Conversations' })).not.toBeInTheDocument()
  })
})
