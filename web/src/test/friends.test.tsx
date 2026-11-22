import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import { parseFriendCode } from '../screens/Friends'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
let overrides: Record<string, unknown> = {}
const calls: { url: string; method: string; body?: unknown }[] = []

function respond(url: URL, method: string): Response {
  // An override applies to every method on its path, unless it is keyed "METHOD /path".
  const key = `${method} ${url.pathname}` in overrides ? `${method} ${url.pathname}` : url.pathname
  if (key in overrides) {
    const o = overrides[key]
    return o instanceof Response ? o.clone() : Response.json(o)
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
    const req = input instanceof Request ? input : null
    const raw = req ? req.url : String(input)
    const text = req ? await req.clone().text() : typeof init?.body === 'string' ? init.body : ''
    calls.push({ url: raw, method: req?.method ?? init?.method ?? 'GET', body: text ? JSON.parse(text) : undefined })
    return respond(new URL(raw, 'http://localhost'), req?.method ?? init?.method ?? 'GET')
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => { vi.unstubAllGlobals(); overrides = {} })

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}

const hub = { name: 'Example Hub', domain: 'hub.example', base_url: 'https://hub.example', policies: {} }
const joined = { available: true, state: 'joined', address: 'mara@hub.example', hub, urls: { instance: 'https://mara.hub.example', files: 'https://files--mara.hub.example' } }
const standalone = { available: true, state: 'standalone' }
const sent = (method: string, path: string) => calls.find((c) => c.method === method && decodeURIComponent(new URL(c.url).pathname) === path)

describe('Friends', () => {
  beforeEach(() => { overrides['/api/membership'] = joined })

  it('lists friends with their presence and badges, requests as cards, and your fingerprint', async () => {
    renderAt('/friends')
    expect(await screen.findByText('Ana')).toBeTruthy()
    expect(screen.getByText('asleep · next: tonight')).toBeTruthy()
    expect(screen.getByText('ana@hub.example · hub.example')).toBeTruthy()
    // Ben is muted and his key changed: both show, with the alarm text.
    const ben = screen.getByRole('button', { name: /Ben, ben@other.example/ })
    expect(within(ben).getByText('Muted')).toBeTruthy()
    expect(within(ben).getByText('Key changed')).toBeTruthy()
    expect(within(ben).getByText(/Their key changed with no rotation/)).toBeTruthy()
    // Cleo's request is a card: address, note, who sent it, and the fingerprint in four groups.
    expect(screen.getByText('Friend request')).toBeTruthy()
    expect(screen.getByText(/Ana said you'd like to compare pump setups/)).toBeTruthy()
    expect(screen.getByText(/sent by their owner/)).toBeTruthy()
    for (const g of ['3H8N', 'V2TP', 'Q6BD', '0YJ4']) expect(screen.getAllByText(g).length).toBeGreaterThan(0)
    expect(screen.getByText(/Compare it with theirs/)).toBeTruthy()
    expect(await screen.findByText(/Your fingerprint · mara@hub.example/)).toBeTruthy()
    expect(screen.getByText('WZCY')).toBeTruthy()
  })

  it('approving a request calls the API', async () => {
    overrides['/api/friends/requests/1/approve'] = { id: 1, direction: 'in', address: 'cleo@hub.example', note: '', made_by: 'owner', code_used: false, state: 'approved', created_at: '2026-11-22T08:00:00Z' }
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await vi.waitFor(() => expect(sent('POST', '/api/friends/requests/1/approve')).toBeTruthy())
  })

  it('declining a request calls the API', async () => {
    overrides['/api/friends/requests/1/decline'] = { id: 1, direction: 'in', address: 'cleo@hub.example', note: '', made_by: 'owner', code_used: false, state: 'declined', created_at: '2026-11-22T08:00:00Z' }
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Decline' }))
    await vi.waitFor(() => expect(sent('POST', '/api/friends/requests/1/decline')).toBeTruthy())
  })

  it('a friend\'s page saves each setting as soon as you pick it', async () => {
    overrides['PATCH /api/friends/ana%40hub.example'] = { ...(table['/api/friends'] as { address: string }[]).find((f) => f.address === 'ana@hub.example'), share: 'anything' }
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: /Ana, ana@hub.example/ }))
    const share = await screen.findByRole('radiogroup', { name: 'What your assistant may share' })
    expect(within(share).getByRole('radio', { name: 'Projects we share' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByText('Save')).toBeNull()
    fireEvent.click(within(share).getByRole('radio', { name: 'Anything' }))
    await vi.waitFor(() => expect(sent('PATCH', '/api/friends/ana@hub.example')?.body).toEqual({ share: 'anything' }))
    fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Waking' })).getByRole('radio', { name: 'Allow' }))
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').at(-1)?.body).toEqual({ waking: 'allow' }))
    fireEvent.click(screen.getByRole('switch', { name: 'Muted' }))
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').at(-1)?.body).toEqual({ muted: true }))
    expect(screen.getByRole('link', { name: 'Open conversation' }).getAttribute('href')).toBe('/t/11')
  })

  it('unfriending asks first', async () => {
    overrides['DELETE /api/friends/ana%40hub.example'] = { ok: true }
    renderAt('/friends/ana%40hub.example')
    fireEvent.click(await screen.findByRole('button', { name: 'Unfriend…' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    expect(screen.getByText(/They're told/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Unfriend' }))
    await vi.waitFor(() => expect(sent('DELETE', '/api/friends/ana@hub.example')).toBeTruthy())
  })

  it('a friend code is shown once: closing the popup forgets it', async () => {
    overrides['POST /api/friends/codes'] = {
      id: 7, code: 'l0wf1:mara@hub.example:WZCY8FG5JZ5EV87N:c2VjcmV0c2VjcmV0c2Vj', address: 'mara@hub.example', fingerprint: 'WZCY-8FG5-JZ5E-V87N', expires_at: '2026-12-06T10:00:00Z',
    }
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Friend code' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Make a code' }))
    const code = await screen.findByTestId('friend-code')
    expect(code.textContent).toBe('l0wf1:mara@hub.example:WZCY8FG5JZ5EV87N:c2VjcmV0c2VjcmV0c2Vj')
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
    expect(screen.getByText(/only time you'll see it/)).toBeTruthy()
    expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ expires_days: 14, label: '' })
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await vi.waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Friend code' }))
    expect(await screen.findByRole('button', { name: 'Make a code' })).toBeTruthy()
    expect(screen.queryByTestId('friend-code')).toBeNull()
  })

  it('pasting a friend code shows its address and fingerprint before sending; a mismatch is explained', async () => {
    overrides['POST /api/friends/requests'] = new Response(JSON.stringify({ error: 'fingerprint_mismatch', detail: "Their key doesn't match the fingerprint in the code." }), { status: 409, headers: { 'content-type': 'application/json' } })
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Add a friend' }))
    const field = await screen.findByLabelText('Friend code or address')
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.change(field, { target: { value: 'l0wf1:dana@hub.example:ABCD2345EFGH6789:c2VjcmV0c2VjcmV0c2Vj' } })
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('dana@hub.example')).toBeTruthy()
    for (const g of ['ABCD', '2345', 'EFGH', '6789']) expect(within(dialog).getByText(g)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'Hi from the club' } })
    expect(send.disabled).toBe(false)
    fireEvent.click(send)
    expect((await screen.findByRole('alert')).textContent).toMatch(/doesn't match the fingerprint.*Nothing was sent/)
    expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ code: 'l0wf1:dana@hub.example:ABCD2345EFGH6789:c2VjcmV0c2VjcmV0c2Vj', note: 'Hi from the club' })
  })

  it('a plain address needs name@hub; an incomplete code is flagged', async () => {
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Add a friend' }))
    const field = await screen.findByLabelText('Friend code or address')
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    fireEvent.change(field, { target: { value: 'dana' } })
    expect(send.disabled).toBe(true)
    fireEvent.change(field, { target: { value: 'dana@hub.example' } })
    expect(send.disabled).toBe(false)
    fireEvent.change(field, { target: { value: 'l0wf1:dana@hub.ex' } })
    expect(send.disabled).toBe(true)
    expect(screen.getByText(/doesn't look like a whole friend code/)).toBeTruthy()
  })

  it('parseFriendCode reads the address and groups the fingerprint', () => {
    expect(parseFriendCode('l0wf1:dana@hub.example:abcd2345efgh6789:c2VjcmV0')).toEqual({ address: 'dana@hub.example', fingerprint: 'ABCD-2345-EFGH-6789' })
    expect(parseFriendCode('l0wf1:dana@hub.example:abcd:c2VjcmV0')).toBeNull()
    expect(parseFriendCode('dana@hub.example')).toBeNull()
  })

  it('says so, with a link to Settings → Hub, when the install has not joined one', async () => {
    overrides['/api/membership'] = standalone
    renderAt('/friends')
    expect(await screen.findByText('Friends need a hub')).toBeTruthy()
    expect(screen.getByRole('link', { name: /Settings → Hub/ }).getAttribute('href')).toBe('/settings/hub')
    expect(calls.some((c) => c.url.includes('/api/friends'))).toBe(false)
  })

  it('shows a banner while friend activity is paused', async () => {
    overrides['/api/friends/settings'] = { requests_from: 'hub', paused: true }
    renderAt('/friends')
    expect(await screen.findByText('Friend activity is paused.')).toBeTruthy()
  })
})

describe('the spend breaker', () => {
  const calm = { tripped: false, paused: false, day_spent: 1.2, day_limit: 7.5, day_pct: 5, day_step_pct: 5, choices: [], spent_on: [] }
  const run = {
    tripped: true, paused: true, limit: 'run', day_spent: 4, day_limit: 7.5, day_pct: 5, day_step_pct: 5, run_id: 123, run_spent: 9.1, run_limit: 8, choices: ['allow_today', 'allow_run', 'stop_today'],
    explanation: 'Run #123 has spent about 9.1 pts on friends, more than 20% (8.0 pts) of its budget of 40 pts. Its replies to friends and wake requests are paused.', spent_on: [], at: '2026-11-22T09:50:00Z',
  }
  beforeEach(() => { overrides['/api/membership'] = joined })

  it('a card at the top says which limit was hit, how much, and offers the choices', async () => {
    renderAt('/friends')
    const card = await screen.findByRole('region', { name: 'Spending on friends hit a limit' })  // the mock is tripped on the day's limit
    expect(within(card).getByText(/spending on friends today is about 8.4 pts, more than 5% \(7.5 pts\)/)).toBeTruthy()
    expect(within(card).getByText(/Today about 8.4 of 7.5 pts \(5% of the allowance\)/)).toBeTruthy()
    expect(within(card).getByRole('button', { name: 'Allow +5% today' })).toBeTruthy()
    expect(within(card).getByRole('button', { name: 'Stop for today' })).toBeTruthy()
    expect(within(card).queryByRole('button', { name: 'Allow for this run' })).toBeNull()  // only when the run limit was hit
    // It comes before the requests and the list.
    expect(card.compareDocumentPosition(screen.getByText('Your friends')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('offers Allow for this run when the run limit was the one hit, and says which run', async () => {
    overrides['/api/friends/breaker'] = run
    overrides['POST /api/friends/breaker/allow-run'] = calm
    renderAt('/friends')
    const card = await screen.findByRole('region', { name: 'Spending on friends hit a limit' })
    expect(within(card).getByText(/Run #123 has spent about 9.1 pts on friends/)).toBeTruthy()
    expect(within(card).getByText(/run #123: 9.1 of 8 pts/)).toBeTruthy()
    overrides['/api/friends/breaker'] = calm  // what it says after the answer
    fireEvent.click(within(card).getByRole('button', { name: 'Allow for this run' }))
    await vi.waitFor(() => expect(sent('POST', '/api/friends/breaker/allow-run')?.body).toMatchObject({ idempotency_key: expect.any(String) }))
    // The card sinks out once it's resolved.
    await vi.waitFor(() => expect(screen.queryByRole('region', { name: 'Spending on friends hit a limit' })).toBeNull())
  })

  it('Allow +5% today and Stop for today each call their own route', async () => {
    overrides['POST /api/friends/breaker/allow-today'] = calm
    overrides['POST /api/friends/breaker/stop-today'] = { ...calm, paused: true, stopped_until: '2026-11-23T00:00:00Z' }
    renderAt('/friends')
    fireEvent.click(await screen.findByRole('button', { name: 'Allow +5% today' }))
    await vi.waitFor(() => expect(sent('POST', '/api/friends/breaker/allow-today')).toBeTruthy())
    fireEvent.click(await screen.findByRole('button', { name: 'Stop for today' }))
    await vi.waitFor(() => expect(sent('POST', '/api/friends/breaker/stop-today')).toBeTruthy())
  })

  it('shows no card when nothing was hit, and a quiet line while sends are stopped for today', async () => {
    overrides['/api/friends/breaker'] = calm
    renderAt('/friends')
    expect(await screen.findByText('Your friends')).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Spending on friends hit a limit' })).toBeNull()
    expect(screen.queryByText(/Replies to friends are stopped/)).toBeNull()
  })

  it('shows a quiet line while sends are stopped for today', async () => {
    overrides['/api/friends/breaker'] = { ...calm, paused: true, stopped_until: '2026-11-23T00:00:00Z' }
    renderAt('/friends')
    expect(await screen.findByText(/Replies to friends are stopped until/)).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Spending on friends hit a limit' })).toBeNull()
  })

  it('a friend\'s page has a Spend limit control that saves as soon as you pick', async () => {
    overrides['PATCH /api/friends/ana%40hub.example'] = { ...(table['/api/friends'] as { address: string }[]).find((f) => f.address === 'ana@hub.example'), breaker: 'off' }
    renderAt('/friends/ana%40hub.example')
    const group = await screen.findByRole('radiogroup', { name: 'Spend limit' })
    expect(within(group).getByRole('radio', { name: 'Normal' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(within(group).getByRole('radio', { name: 'No limit' }))
    await vi.waitFor(() => expect(sent('PATCH', '/api/friends/ana@hub.example')?.body).toEqual({ breaker: 'off' }))
  })
})

describe('the nav entry', () => {
  it('is in More once the install has joined a hub', async () => {
    overrides['/api/membership'] = joined
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: /More/ }))
    expect(await screen.findByText('Friends')).toBeTruthy()
    expect(screen.getByText('Network')).toBeTruthy()
  })

  it('is not there when the install is not joined', async () => {
    overrides['/api/membership'] = standalone
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: /More/ }))
    expect(await screen.findByText('Network')).toBeTruthy()
    expect(screen.queryByText('Friends')).toBeNull()
  })
})

describe('Settings → Hub → Friends', () => {
  it('saves who may send requests and the pause switch on change', async () => {
    overrides['/api/membership'] = { ...joined, tunnel: { state: 'connected' }, sign_key: { kid: 'abcdef0123456789', created: '2026-11-18T09:00:00Z' } }
    overrides['/api/friends/settings'] = { requests_from: 'hub', paused: false }
    renderAt('/settings/hub')
    const who = await screen.findByRole('radiogroup', { name: 'Who can send you friend requests' })
    expect(within(who).getByRole('radio', { name: 'Members of my hub' }).getAttribute('aria-checked')).toBe('true')
    fireEvent.click(within(who).getByRole('radio', { name: 'Only with a friend code' }))
    await vi.waitFor(() => expect(sent('PATCH', '/api/friends/settings')?.body).toEqual({ requests_from: 'codes' }))
    fireEvent.click(screen.getByRole('switch', { name: 'Pause all friend activity' }))
    await vi.waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').at(-1)?.body).toEqual({ paused: true }))
  })
})

describe('a thread with a friend', () => {
  const thread = { id: 11, project: null, title: 'ana@hub.example', type: 'peer', status: 'open', created_by: 'owner', pinned: false, created_at: '2026-11-02T10:00:00Z', last_activity_at: '2026-11-22T09:00:00Z', last_message_preview: null, unread: 0, open_asks: 0 }
  const msg = (id: number, sender: string, body: string) => ({ type: 'message', message: { id, thread_id: 11, sender, body, via: sender, run_id: null, attachments: [], delivery: 'delivered', created_at: '2026-11-22T09:00:00Z' } })

  it('labels their messages as untrusted, plain text, with their name; the header links to their page', async () => {
    overrides['/api/membership'] = joined
    overrides['/api/threads/11'] = thread
    overrides['/api/threads/11/items'] = { items: [msg(1, 'owner', 'Which relay?'), msg(2, 'peer', 'It is the **SRD-05VDC**. [click](https://evil.example)')], next_cursor: null }
    renderAt('/t/11')
    expect(await screen.findByText('friend · untrusted')).toBeTruthy()
    expect((await screen.findAllByText('Ana')).length).toBeGreaterThan(0)
    // Plain text: no Markdown, so no link or bold.
    expect(screen.getByText('It is the **SRD-05VDC**. [click](https://evil.example)')).toBeTruthy()
    expect(document.querySelector('a[href="https://evil.example"]')).toBeNull()
    expect(screen.getByRole('link', { name: 'Friend settings' }).getAttribute('href')).toBe('/friends/ana%40hub.example')
    expect(await screen.findByPlaceholderText("Message Ana's assistant…")).toBeTruthy()
  })
})
