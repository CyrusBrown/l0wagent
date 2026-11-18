import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  localStorage.clear()
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
    expect(await screen.findByText(/Reproduced the double trigger/)).toBeInTheDocument()
  })
  it('Files', async () => {
    renderAt('/files')
    expect((await screen.findAllByText('NOW.md')).length).toBe(2) // tree and table
    expect(screen.getByRole('button', { name: 'Upload to a project' })).toBeInTheDocument()
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
  it('Settings: Appearance switches between 24-hour and 12-hour time', async () => {
    renderAt('/settings/appearance')
    const twelve = await screen.findByRole('button', { name: /12-hour/ })
    const twentyFour = screen.getByRole('button', { name: /24-hour/ })
    expect(twentyFour).toHaveAttribute('aria-pressed', 'true')
    expect(twelve).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(twelve)
    expect(twelve).toHaveAttribute('aria-pressed', 'true')
    await waitFor(() => expect(localStorage.getItem('l0w.timeFormat')).toBe('12h'))
    fireEvent.click(twentyFour)
    expect(twentyFour).toHaveAttribute('aria-pressed', 'true')
    await waitFor(() => expect(localStorage.getItem('l0w.timeFormat')).toBe('24h'))
  })
  it('Settings: extensions, none installed', async () => {
    overrides['/api/extensions'] = []
    renderAt('/settings/extensions')
    expect(await screen.findByText('No extensions installed.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rescan' })).toBeInTheDocument()
  })
  it('Settings: budgets and levels', async () => {
    renderAt('/settings/budgets')
    expect(await screen.findByRole('button', { name: /Steady\s*150 pts \/ week/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText("Steady's limits")).toBeInTheDocument()
    expect(screen.getByText('Allowance')).toBeInTheDocument()
    expect(screen.getByText('Always, whatever the level')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Boost for 24 h' })).toBeInTheDocument()
  })
  describe('budget limits draft', () => {
    type Call = { method: string; path: string; body: unknown }
    function setup(custom: string[]) {
      const b = structuredClone((table['/api/budget'] as { providers: Record<string, unknown>[] }))
      b.providers[0].custom = custom
      overrides['/api/budget'] = b
      const calls: Call[] = []
      const base = globalThis.fetch as unknown as (i: RequestInfo | URL) => Promise<Response>
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = input instanceof Request ? input : null
        const method = (req?.method ?? init?.method ?? 'GET').toUpperCase()
        if (method !== 'GET') {
          const path = new URL(req?.url ?? String(input), 'http://localhost').pathname
          calls.push({ method, path, body: JSON.parse((await req?.clone().text()) || String(init?.body ?? 'null')) })
          return Response.json({})
        }
        return base(input)
      }))
      return calls
    }
    it('edits are held until Save, which sends one PATCH; reserve fields go to /api/budget', async () => {
      const calls = setup([])
      renderAt('/settings/budgets')
      const gap = await screen.findByLabelText('Wakeups a day')
      expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
      fireEvent.change(gap, { target: { value: '5' } }); fireEvent.blur(gap)
      fireEvent.click(screen.getByRole('switch', { name: 'Sweep' }))
      fireEvent.change(await screen.findByLabelText('Sweep window'), { target: { value: '6.5' } }); fireEvent.blur(screen.getByLabelText('Sweep window'))
      expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
      expect(screen.getByText(/in the last 6.5 h before the reset/)).toBeInTheDocument()
      expect(calls).toEqual([])
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(calls.length).toBe(2))
      expect(calls[0]).toMatchObject({ method: 'PATCH', body: { overrides: { max_per_day: 5, sweep: true } } })
      expect(calls[1]).toMatchObject({ method: 'PATCH', path: '/api/budget', body: { sweep_window_hours: 6.5 } })
      await waitFor(() => expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument())
    })
    it('Discard restores the saved values', async () => {
      const calls = setup([])
      renderAt('/settings/budgets')
      const f = await screen.findByLabelText('Wakeups a day') as HTMLInputElement
      const before = f.value
      fireEvent.change(f, { target: { value: '9' } }); fireEvent.blur(f)
      fireEvent.click(await screen.findByRole('button', { name: 'Discard' }))
      await waitFor(() => expect((screen.getByLabelText('Wakeups a day') as HTMLInputElement).value).toBe(before))
      expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument()
      expect(calls).toEqual([])
    })
    it('no banner; a Modified line with Reset to default when limits are customized', async () => {
      setup(['max_per_day'])
      renderAt('/settings/budgets')
      expect(await screen.findByText(/Modified from Steady's defaults: wakeup limits\./)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Reset to default' })).toBeInTheDocument()
      expect(screen.queryByText(/You customized/)).not.toBeInTheDocument()
      // a row's Reset changes the draft only
      fireEvent.click(screen.getAllByRole('button', { name: 'Reset' })[0])
      expect(await screen.findByText('Unsaved changes')).toBeInTheDocument()
    })
  })
  it('Calendar: planned wakeups, routines and the budget', async () => {
    renderAt('/calendar')
    expect(await screen.findByRole('heading', { name: 'Calendar' })).toBeInTheDocument()
    expect((await screen.findAllByText('Finish the pump fix, then the receipts')).length).toBeGreaterThan(0)
    expect(screen.getByText('Budget left this week')).toBeInTheDocument()
    expect((await screen.findAllByText('Inbox triage')).length).toBeGreaterThan(0)
  })
  it('Calendar: Escape closes the wakeup editor, a nested picker first', async () => {
    renderAt('/calendar')
    const add = await screen.findByRole('button', { name: '+ Add wakeup' })
    add.focus()
    fireEvent.click(add)
    const dialog = screen.getByRole('dialog', { name: 'Add wakeup' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const tier = screen.getByRole('button', { name: /^Tier: / })
    fireEvent.click(tier)
    expect(screen.getByRole('listbox', { name: 'Tier' })).toBeInTheDocument()
    fireEvent.keyDown(tier, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Tier' })).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Add wakeup' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add wakeup' })).not.toBeInTheDocument())
    expect(add).toHaveFocus()
  })
  it('Calendar: focus moves into the wakeup editor and Tab wraps inside it', async () => {
    renderAt('/calendar')
    const add = await screen.findByRole('button', { name: '+ Add wakeup' })
    add.focus()
    fireEvent.click(add)
    const dialog = screen.getByRole('dialog', { name: 'Add wakeup' })
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement))
    const items = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled])'))
    const last = items[items.length - 1]
    last.focus()
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(items[0]).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
  })
  it('Calendar: the hour axis follows the 12-hour setting', async () => {
    localStorage.setItem('l0w.timeFormat', '12h')
    renderAt('/calendar')
    expect(await screen.findByText('3 AM')).toBeInTheDocument()
    expect(screen.getByText('12 AM')).toBeInTheDocument()
    expect(screen.getByText('9 PM')).toBeInTheDocument()
  })
  it('Shell: the status chip repaints when the time format changes', async () => {
    const at = new Date(Date.now() + 3 * 86400_000).toISOString()
    overrides['/api/status'] = { ...(table['/api/status'] as object), state: 'asleep', next_wakeup_at: at }
    renderAt('/settings/appearance')
    const chip = await screen.findByTitle('Open runs')
    await waitFor(() => expect(chip).toHaveTextContent('next wakeup'))
    expect(chip.textContent).not.toMatch(/[AP]M/)
    fireEvent.click(await screen.findByRole('button', { name: /12-hour/ }))
    await waitFor(() => expect(chip.textContent).toMatch(/[AP]M/))
  })
  it('Calendar: the routine switch renders and toggles', async () => {
    renderAt('/calendar')
    const sw = await screen.findByRole('switch', { name: 'Inbox triage on' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(sw)
    await waitFor(async () => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) =>
        input instanceof Request && input.method === 'PATCH' && new URL(input.url).pathname === '/api/routines/2')
      expect(call).toBeTruthy()
      expect(await (call![0] as Request).clone().json()).toEqual({ enabled: false })
    })
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
  it('Network: the log, top hosts and rules', async () => {
    renderAt('/network')
    expect(await screen.findByRole('heading', { name: 'Network' })).toBeInTheDocument()
    expect(await screen.findByText(/361 requests · 3 blocked/)).toBeInTheDocument()
    expect(screen.getByText('tracker.example-ads.net/pixel').closest('.net-row')).toHaveClass('blocked')
    expect(screen.getByText('Allow read-only')).toBeInTheDocument()
    expect(screen.getByText(/Added by approving permission #4/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '+ Add rule' }))
    expect(screen.getByLabelText('Host or pattern')).toBeInTheDocument()
  })
  it('Tools & Secrets: the Network rules tab', async () => {
    renderAt('/tools?tab=network')
    expect(await screen.findByText('*.github.com')).toBeInTheDocument()
    expect(screen.getByText('212 requests · 7 d')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /full request log/ })).toHaveAttribute('href', '/network')
  })
  it('New', async () => {
    renderAt('/new')
    expect(await screen.findByPlaceholderText('What should the assistant work on?')).toBeInTheDocument()
    const chat = screen.getByRole('button', { name: '1:1 chat' })
    expect(chat).toBeEnabled()
    fireEvent.click(chat)
    expect(await screen.findByPlaceholderText('Start a chat. Replies stream right away.')).toBeInTheDocument()
  })
  it('Project tabs', async () => {
    renderAt('/p/garden-irrigation')
    fireEvent.click(await screen.findByRole('button', { name: /^Asks/ }))
    expect((await screen.findAllByText(/Should I flash the fix|Write access/)).length).toBeGreaterThan(0)
  })
})
