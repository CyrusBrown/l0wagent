import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { S } from '../api/client'
import { Palette } from '../app/Palette'
import { ModelPicker } from '../components/settings'
import responses from './mock-responses.json'

const table = responses as Record<string, unknown>

function respond(url: URL): Response {
  if (url.pathname === '/api/events') return new Response(new ReadableStream())
  const exact = table[url.pathname + url.search]
  const loose = Object.entries(table).find(([k]) => k.split('?')[0] === url.pathname)?.[1]
  const body = exact ?? loose
  return body === undefined ? new Response('{"error":"not_found"}', { status: 404 }) : Response.json(body)
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = input instanceof Request ? input.url : String(input)
    return respond(new URL(raw, 'http://localhost'))
  }))
})
afterEach(() => { vi.unstubAllGlobals() })

const providers: S['ProviderInfo'][] = [
  { id: 'anthropic-api', kind: 'anthropic-api', label: 'Anthropic API', enabled: true, status: '', usage_source: 'api', has_credential: true, models: null },
]

function renderPicker() {
  const onChange = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}>
    <ModelPicker providers={providers} provider="anthropic-api" tier="sonnet/medium" onChange={onChange} label="Worker model" />
  </QueryClientProvider>)
  return onChange
}

describe('Combo (model picker)', () => {
  it('closes with Escape from the search and returns focus to the trigger', async () => {
    renderPicker()
    const trigger = screen.getByRole('button', { name: 'Worker model' })
    trigger.focus()
    fireEvent.click(trigger)
    const search = screen.getByRole('combobox', { name: 'Search Worker model' })
    expect(search).toHaveFocus()
    fireEvent.keyDown(search, { key: 'Escape', bubbles: true })
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Worker model' })).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('closes with Escape while an option has focus', async () => {
    renderPicker()
    const trigger = screen.getByRole('button', { name: 'Worker model' })
    trigger.focus()
    fireEvent.click(trigger)
    const option = screen.getByRole('option', { name: 'Opus' })
    option.focus()
    expect(option).toHaveFocus()
    fireEvent.keyDown(option, { key: 'Escape', bubbles: true })
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Worker model' })).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('navigates with ArrowDown, Home, End and Enter, exposing the active option', async () => {
    const onChange = renderPicker()
    const trigger = screen.getByRole('button', { name: 'Worker model' })
    fireEvent.click(trigger)
    const search = screen.getByRole('combobox', { name: 'Search Worker model' })
    const idOf = (name: string) => screen.getByRole('option', { name }).id
    expect(search).toHaveAttribute('aria-activedescendant', idOf('Sonnet'))
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    expect(search).toHaveAttribute('aria-activedescendant', idOf('Opus'))
    fireEvent.keyDown(search, { key: 'ArrowUp' })
    expect(search).toHaveAttribute('aria-activedescendant', idOf('Sonnet'))
    fireEvent.keyDown(search, { key: 'Home' })
    expect(search).toHaveAttribute('aria-activedescendant', idOf('Haiku'))
    fireEvent.keyDown(search, { key: 'End' })
    expect(search).toHaveAttribute('aria-activedescendant', idOf('Opus'))
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('anthropic-api', 'opus/medium')
    await waitFor(() => expect(screen.queryByRole('listbox', { name: 'Worker model' })).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})

function renderPalette() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Harness() {
    const [open, setOpen] = useState(false)
    return <>
      <button onClick={() => setOpen(true)}>Opener</button>
      {open && <Palette onClose={() => setOpen(false)} />}
    </>
  }
  render(<QueryClientProvider client={qc}><RouterProvider router={createMemoryRouter([{ path: '/', element: <Harness /> }])} /></QueryClientProvider>)
  const opener = screen.getByRole('button', { name: 'Opener' })
  opener.focus()
  fireEvent.click(opener)
  return opener
}

describe('Palette', () => {
  it('closes with Escape from a result and restores focus to the opener', async () => {
    const opener = renderPalette()
    const dialog = await screen.findByRole('dialog', { name: 'Search' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const result = await screen.findByRole('button', { name: /Settings/ })
    result.focus()
    expect(result).toHaveFocus()
    fireEvent.keyDown(result, { key: 'Escape', bubbles: true })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Search' })).not.toBeInTheDocument())
    expect(opener).toHaveFocus()
  })
})
