import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { routes } from '../app/routes'
import { guessName } from '../screens/SettingsExtensions'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>
let overrides: Record<string, unknown> = {}
const calls: { url: string; method: string; body: string }[] = []

function respond(url: URL): Response {
  if (url.pathname in overrides) {
    const o = overrides[url.pathname]
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
    const raw = input instanceof Request ? input.url : String(input)
    const body = input instanceof Request ? await input.clone().text() : String(init?.body ?? '')
    calls.push({ url: raw, method: input instanceof Request ? input.method : init?.method ?? 'GET', body })
    return respond(new URL(raw, 'http://localhost'))
  }))
  window.matchMedia ??= (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never
})
afterEach(() => { vi.unstubAllGlobals(); overrides = {} })

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} /></QueryClientProvider>)
}
const sent = (method: string, suffix: string) => calls.find((c) => c.method === method && decodeURIComponent(new URL(c.url, 'http://x').pathname) === suffix)
const update = (over: object = {}) => ({ ...(table['/api/system/update'] as object), ...over })

describe('the update banner', () => {
  it('Update now asks first, then starts the update for the offered version', async () => {
    overrides['/api/system/update/apply'] = update({ applying: { version: '0.5.0', state: 'waiting', step: 'Waiting for the assistant', started_at: '2026-11-22T10:00:00Z' } })
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: 'Update now' }))
    expect(screen.getByText(/The assistant finishes what it's doing first; the app restarts and reconnects\./)).toBeInTheDocument()
    expect(sent('POST', '/api/system/update/apply')).toBeUndefined()  // nothing until it is confirmed
    fireEvent.click(screen.getByRole('button', { name: 'Update now' }))
    await waitFor(() => expect(sent('POST', '/api/system/update/apply')).toBeDefined())
    const body = JSON.parse(sent('POST', '/api/system/update/apply')!.body)
    expect(body.version).toBe('0.5.0')
    expect(body.idempotency_key).toBeTruthy()
  })
  it('Not now backs out of the confirm step', async () => {
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: 'Update now' }))
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
    expect(await screen.findByRole('button', { name: 'Update now' })).toBeInTheDocument()
    expect(sent('POST', '/api/system/update/apply')).toBeUndefined()
  })
  it('shows the refusal in words', async () => {
    overrides['/api/system/update/apply'] = new Response(JSON.stringify({ detail: 'Updating from the web app is off.' }), { status: 409 })
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: 'Update now' }))
    fireEvent.click(screen.getByRole('button', { name: 'Update now' }))
    expect(await screen.findByText('Updating from the web app is off.')).toBeInTheDocument()
  })
  it('without the ops update option it points at the server, as before', async () => {
    overrides['/api/system/update'] = update({ ops_docker: false })
    renderAt('/')
    fireEvent.click(await screen.findByRole('button', { name: 'Update after the current run' }))
    expect(screen.getByText('./l0w update')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update now' })).toBeNull()
  })
  it('shows each step of an update in progress, and Cancel only while it waits', async () => {
    overrides['/api/system/update'] = update({ applying: { version: '0.5.0', state: 'waiting', step: 'Waiting for the assistant: run #7 is in progress', started_at: '2026-11-22T10:00:00Z' } })
    renderAt('/')
    expect(await screen.findByText('Updating to l0wagent 0.5.0')).toBeInTheDocument()
    const steps = screen.getByRole('list', { name: 'Update progress' })
    expect(within(steps).getByText('Waiting for the assistant').getAttribute('aria-current')).toBe('step')
    expect(screen.getByText(/run #7 is in progress/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel update' }))
    await waitFor(() => expect(sent('POST', '/api/system/update/cancel')).toBeDefined())
  })
  it('restarting has no Cancel and the steps before it are done', async () => {
    overrides['/api/system/update'] = update({ applying: { version: '0.5.0', state: 'restarting', step: 'Restarting the app', started_at: '2026-11-22T10:00:00Z' } })
    renderAt('/')
    const steps = await screen.findByRole('list', { name: 'Update progress' })
    expect(within(steps).getByText('Restarting').getAttribute('aria-current')).toBe('step')
    expect(within(steps).getByText('Updating').className).toBe('done')
    expect(screen.queryByRole('button', { name: 'Cancel update' })).toBeNull()
  })
  it('after the restart it says how the update ended, once', async () => {
    overrides['/api/system/update'] = update({ available: false, current: '0.5.0', applying: null, last_result: { version: '0.5.0', ok: true, error: null, at: '2026-11-22T10:20:00Z' } })
    renderAt('/')
    expect(await screen.findByText('Updated to l0wagent 0.5.0')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload the app' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('Updated to l0wagent 0.5.0')).toBeNull())
  })
  it('a failed update shows why, and stays dismissed once dismissed', async () => {
    overrides['/api/system/update'] = update({ applying: null, last_result: { version: '0.5.0', ok: false, error: 'pull access denied for l0wagent-control', at: '2026-11-22T10:20:00Z' } })
    renderAt('/')
    expect(await screen.findByText('The update to 0.5.0 failed')).toBeInTheDocument()
    expect(screen.getByText('pull access denied for l0wagent-control')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('The update to 0.5.0 failed')).toBeNull())
    expect(localStorage.getItem('l0w-update-result-2026-11-22T10:20:00Z')).toBe('1')
  })
})

describe('Settings → Extensions: install from git', () => {
  it('resolves the ref to a commit, shows it, then installs that commit', async () => {
    overrides['/api/extensions'] = []
    overrides['/api/extensions/resolve'] = { commit: 'ab'.repeat(20) }
    overrides['/api/extensions/install'] = Response.json({ name: 'plantlog' }, { status: 201 })
    renderAt('/settings/extensions')
    fireEvent.click(await screen.findByRole('button', { name: 'Install from git…' }))
    fireEvent.change(screen.getByLabelText('Git address'), { target: { value: 'https://github.com/you/plantlog.git' } })
    fireEvent.change(screen.getByLabelText('Branch, tag or commit'), { target: { value: 'v1.0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    expect((await screen.findByTestId('resolved-commit')).textContent).toBe('ab'.repeat(20))
    expect(JSON.parse(sent('POST', '/api/extensions/resolve')!.body)).toEqual({ git: 'https://github.com/you/plantlog.git', ref: 'v1.0' })
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe('plantlog')  // a guess from the address
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => expect(sent('POST', '/api/extensions/install')).toBeDefined())
    expect(JSON.parse(sent('POST', '/api/extensions/install')!.body)).toMatchObject({ name: 'plantlog', git: 'https://github.com/you/plantlog.git', commit: 'ab'.repeat(20), path: null })
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Install from git' })).toBeNull())  // done: the form closes
  })
  it('changing the address after resolving asks for a new commit', async () => {
    overrides['/api/extensions'] = []
    overrides['/api/extensions/resolve'] = { commit: 'cd'.repeat(20) }
    renderAt('/settings/extensions')
    fireEvent.click(await screen.findByRole('button', { name: 'Install from git…' }))
    fireEvent.change(screen.getByLabelText('Git address'), { target: { value: 'https://example.com/a.git' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    await screen.findByTestId('resolved-commit')
    fireEvent.change(screen.getByLabelText('Git address'), { target: { value: 'https://example.com/b.git' } })
    expect(screen.queryByTestId('resolved-commit')).toBeNull()
    expect(screen.getByRole('button', { name: 'Resolve' })).toBeInTheDocument()
  })
  it("shows ops' refusal", async () => {
    overrides['/api/extensions'] = []
    overrides['/api/extensions/resolve'] = new Response(JSON.stringify({ detail: 'git must be an https URL' }), { status: 400 })
    renderAt('/settings/extensions')
    fireEvent.click(await screen.findByRole('button', { name: 'Install from git…' }))
    fireEvent.change(screen.getByLabelText('Git address'), { target: { value: 'ssh://x/y' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
    expect(await screen.findByText('git must be an https URL')).toBeInTheDocument()
  })
  it('is hidden when this install has no ops container', async () => {
    overrides['/api/meta'] = { ...(table['/api/meta'] as { capabilities: object }), capabilities: { ...(table['/api/meta'] as { capabilities: object }).capabilities, ops: false } }
    overrides['/api/extensions'] = []
    renderAt('/settings/extensions')
    await screen.findByText('No extensions installed.')
    expect(screen.queryByRole('button', { name: 'Install from git…' })).toBeNull()
  })
  it('guesses a name from the address', () => {
    expect(guessName('https://github.com/you/Plant_Log.git')).toBe('plant-log')
    expect(guessName('https://git.example/x/plantlog/')).toBe('plantlog')
    expect(guessName('https://git.example/x/2fa.git')).toBe('fa')
  })

  const ext = (over: object) => ({
    name: 'plantlog', title: 'Plant log', version: '1.0.0', sdk: '1.0', source: 'https://g/x.git@abc', description: '', enabled: false, loaded: false, present: true, compatible: true,
    last_error: null, settings: [], commands: [], folders: [], prompt_slots: [], ui: null, routines: [], ...over,
  })
  it('an extension installed from the web app can be removed, after asking', async () => {
    overrides['/api/extensions'] = [ext({ ops: { source: 'owner', git: 'https://g/x.git', commit: 'a'.repeat(40), path: null } })]
    overrides['/api/extensions/plantlog'] = { name: 'plantlog', still_in_extensions_toml: true }
    renderAt('/settings/extensions')
    fireEvent.click(await screen.findByRole('button', { name: 'Remove…' }))
    expect(sent('DELETE', '/api/extensions/plantlog')).toBeUndefined()
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(sent('DELETE', '/api/extensions/plantlog')).toBeDefined())
    expect(await screen.findByText(/Your extensions.toml still lists it/)).toBeInTheDocument()
  })
  it('a hub-installed extension is tagged with its hub and has no Remove here; a local one has none either', async () => {
    overrides['/api/membership'] = { available: true, state: 'joined', address: 'fern@hub.example', hub: { name: 'Example Hub', domain: 'hub.example', base_url: 'https://hub.example', policies: {} } }
    overrides['/api/extensions'] = [ext({ name: 'board', title: 'Board', ops: { source: 'hub', git: 'https://g/x.git', commit: 'a'.repeat(40), path: 'extensions/board' } }), ext({ name: 'mine', title: 'Mine' })]
    renderAt('/settings/extensions')
    expect(await screen.findByText('from Example Hub')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove…' })).toBeNull()
  })
})

describe('Settings → Hub: services', () => {
  const joined = (over: object = {}) => ({
    available: true, state: 'joined', address: 'fern@hub.example', hub: { name: 'Example Hub', domain: 'hub.example', base_url: 'https://hub.example', policies: {} },
    urls: { instance: 'https://fern.hub.example', files: 'https://f.hub.example' }, tunnel: { state: 'connected' }, sign_key: { kid: 'abcdef0123456789', created: '2026-11-18T09:00:00Z' },
    ask_before_extension_updates: false, ...over,
  })
  const service = (over: object) => ({
    address: 'echo@hub.example', kind: 'custom', title: 'Echo', description: 'A test service.', roles: ['tester'], roles_held: ['tester'], default_on: true, enabled: true,
    state: 'connected', updated_at: '2026-11-22T10:00:00Z', extension: null, extension_enabled: true, ...over,
  })
  const board = (over: object = {}) => service({
    address: 'board@hub.example', kind: 'board', title: 'Board', description: 'Topics and posts.', roles: ['poster'], roles_held: [], enabled: false, state: 'off',
    extension: { git: 'https://g/x.git', commit: 'b'.repeat(40), path: 'extensions/board' }, extension_name: 'board', extension_state: 'installed', extension_commit: 'a'.repeat(40), ...over,
  })

  it('lists the hub\'s services with their state and roles, and each switch saves on change', async () => {
    overrides['/api/membership'] = joined()
    overrides['/api/membership/services'] = [service({}), board()]
    overrides['/api/membership/services/echo@hub.example'] = service({ enabled: false })
    renderAt('/settings/hub')
    const echo = await screen.findByTestId('svc-echo@hub.example')
    expect(within(echo).getByText('Echo')).toBeInTheDocument()
    expect(within(echo).getByText(/roles held: tester/)).toBeInTheDocument()
    expect(within(echo).getByText('connected')).toBeInTheDocument()
    fireEvent.click(within(echo).getByRole('switch', { name: 'Echo on' }))
    await waitFor(() => expect(sent('PATCH', '/api/membership/services/echo@hub.example')).toBeDefined())
    expect(JSON.parse(sent('PATCH', '/api/membership/services/echo@hub.example')!.body)).toEqual({ enabled: false })
  })
  it('a service with an extension has its own switch, its commit and its state', async () => {
    overrides['/api/membership'] = joined()
    overrides['/api/membership/services'] = [board()]
    overrides['/api/membership/services/board@hub.example'] = board({ extension_enabled: false, extension_state: 'off' })
    renderAt('/settings/hub')
    const row = await screen.findByTestId('svc-board@hub.example')
    expect(within(row).getByText('board', { selector: 'code' })).toBeInTheDocument()
    expect(within(row).getByText('Installed')).toBeInTheDocument()
    expect(within(row).getByText('a'.repeat(10))).toBeInTheDocument()
    expect(within(row).getByRole('switch', { name: 'Board on' }).getAttribute('aria-checked')).toBe('false')  // the service's own switch is separate
    fireEvent.click(within(row).getByRole('switch', { name: 'Board extension on' }))
    await waitFor(() => expect(sent('PATCH', '/api/membership/services/board@hub.example')).toBeDefined())
    expect(JSON.parse(sent('PATCH', '/api/membership/services/board@hub.example')!.body)).toEqual({ extension_enabled: false })
  })
  it('a waiting update offers Update now, and an install error is shown', async () => {
    overrides['/api/membership'] = joined()
    overrides['/api/membership/services'] = [board({ extension_state: 'waiting' }), service({ address: 'wiki@hub.example', title: 'Wiki', extension: { git: 'g', commit: 'c'.repeat(40), path: '.' }, extension_name: 'wiki', extension_state: 'error', extension_error: 'doc_mismatch: git, commit and path must match' })]
    overrides['/api/membership/services/board@hub.example'] = board()
    renderAt('/settings/hub')
    const row = await screen.findByTestId('svc-board@hub.example')
    expect(within(row).getByText('Newer version waiting for you')).toBeInTheDocument()
    fireEvent.click(within(row).getByRole('button', { name: 'Update now' }))
    await waitFor(() => expect(sent('PATCH', '/api/membership/services/board@hub.example')).toBeDefined())
    expect(JSON.parse(sent('PATCH', '/api/membership/services/board@hub.example')!.body)).toEqual({ extension_enabled: true })
    expect(within(screen.getByTestId('svc-wiki@hub.example')).getByText(/doc_mismatch/)).toBeInTheDocument()
  })
  it('"Ask before a hub updates its extensions" saves on change', async () => {
    overrides['/api/membership'] = joined()
    overrides['/api/membership/services'] = [board()]
    renderAt('/settings/hub')
    const sw = await screen.findByRole('switch', { name: 'Ask before a hub updates its extensions' })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(sw)
    await waitFor(() => expect(sent('PATCH', '/api/membership')).toBeDefined())
    expect(JSON.parse(sent('PATCH', '/api/membership')!.body)).toEqual({ ask_before_extension_updates: true })
  })
  it('without the ops container the extension says so and its switch does nothing', async () => {
    overrides['/api/meta'] = { ...(table['/api/meta'] as { capabilities: object }), capabilities: { ...(table['/api/meta'] as { capabilities: object }).capabilities, ops: false } }
    overrides['/api/membership'] = joined()
    overrides['/api/membership/services'] = [board({ extension_state: 'off', extension_commit: null })]
    renderAt('/settings/hub')
    const row = await screen.findByTestId('svc-board@hub.example')
    expect(within(row).getByText(/needs the ops container/)).toBeInTheDocument()
    fireEvent.click(within(row).getByRole('switch', { name: 'Board extension on' }))
    expect(sent('PATCH', '/api/membership/services/board@hub.example')).toBeUndefined()
    expect(screen.queryByRole('switch', { name: 'Ask before a hub updates its extensions' })).toBeNull()
  })
})
