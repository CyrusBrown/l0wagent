import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { S } from '../api/client'
import { CopyButton } from '../components/CopyButton'
import { ShelfView } from '../components/ShelfView'

const writeText = vi.fn<(text: string) => Promise<void>>()

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined)
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => { vi.unstubAllGlobals() })

const shelfItem: S['ShelfItem'] = {
  id: 7,
  project: 'garden',
  label: 'Notes',
  volume: 'workspace',
  path: 'notes/a.md',
  mode: 'snapshot',
  keep_versions: 0,
  versions: 0,
  added_by: 'assistant',
  run_id: null,
  created_at: '2026-11-16T10:00:00Z',
  updated_at: '2026-11-16T10:00:00Z',
  size: 9,
  mime: 'text/markdown',
  missing: false,
}

function renderShelf(compact: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={createMemoryRouter([{ path: '/', element: <ShelfView project="garden" compact={compact} /> }])} />
    </QueryClientProvider>,
  )
}

function stubShelfFetch(body: string) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
    if (url.pathname === '/api/projects/garden/shelf') return Response.json([shelfItem])
    if (url.pathname === '/api/shelf/7/raw') return new Response(body)
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('CopyButton', () => {
  it('copies a file source\'s contents from its raw endpoint', async () => {
    const fetchMock = vi.fn(async () => new Response('hello\n'))
    vi.stubGlobal('fetch', fetchMock)
    render(<CopyButton src={{ kind: 'file', volume: 'work', path: 'notes/a.md' }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy contents' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello\n'))
    expect(fetchMock).toHaveBeenCalledWith('/api/files/raw?volume=work&path=notes%2Fa.md', { credentials: 'include' })
  })

  it('copies and shows a file source\'s full path', async () => {
    render(<CopyButton src={{ kind: 'file', volume: 'work', path: 'notes/a.md' }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const pathItem = screen.getByRole('button', { name: /^Copy path/ })
    expect(pathItem).toHaveTextContent('/work/notes/a.md')
    fireEvent.click(pathItem)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/work/notes/a.md'))
  })

  it('copies a kept shelf version\'s contents and shows the volume-qualified path', async () => {
    const fetchMock = vi.fn(async () => new Response('v2 text'))
    vi.stubGlobal('fetch', fetchMock)
    render(<CopyButton src={{ kind: 'shelf', id: 7, path: 'notes/a.md', version: 2 }} volume="workspace" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(screen.getByRole('button', { name: /^Copy path/ })).toHaveTextContent('/workspace/notes/a.md')
    fireEvent.click(screen.getByRole('button', { name: 'Copy contents' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('v2 text'))
    expect(fetchMock).toHaveBeenCalledWith('/api/shelf/7/raw?version=2', { credentials: 'include' })
  })

  it('portals the menu out of a clipping container and still copies', async () => {
    const fetchMock = vi.fn(async () => new Response('hello\n'))
    vi.stubGlobal('fetch', fetchMock)
    const { container } = render(
      <div className="shelf-preview" style={{ overflow: 'hidden' }}>
        <CopyButton src={{ kind: 'file', volume: 'work', path: 'notes/a.md' }} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const menu = screen.getByRole('button', { name: 'Copy contents' }).closest<HTMLElement>('.menu')!
    expect(menu.parentElement).toBe(document.body)
    expect(container.querySelector('.shelf-preview')).not.toContainElement(menu)
    expect(container.querySelector('.menu-scrim')).toBeNull()
    expect(document.querySelector('.menu-scrim')?.parentElement).toBe(document.body)
    fireEvent.click(screen.getByRole('button', { name: 'Copy contents' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello\n'))
  })
})

describe('ShelfView copy', () => {
  it('renders the copy control on a full preview and copies its contents', async () => {
    stubShelfFetch('shelf text')
    renderShelf(false)
    const copy = await screen.findByRole('button', { name: 'Copy' })
    expect(copy).toHaveClass('btn', 'small')
    fireEvent.click(copy)
    fireEvent.click(screen.getByRole('button', { name: 'Copy contents' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('shelf text'))
  })

  it('renders the copy control on a compact preview and copies its path', async () => {
    stubShelfFetch('shelf text')
    renderShelf(true)
    const copy = await screen.findByRole('button', { name: 'Copy' })
    expect(copy).toHaveClass('btn', 'small')
    fireEvent.click(copy)
    const pathItem = screen.getByRole('button', { name: /^Copy path/ })
    expect(pathItem).toHaveTextContent('/workspace/notes/a.md')
    fireEvent.click(pathItem)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('/workspace/notes/a.md'))
  })
  it('resets the version when another shelf item is selected', async () => {
    const live = { ...shelfItem, mode: 'live' as const, versions: 2 }
    const other = { ...live, id: 8, label: 'Other', path: 'notes/b.md' }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), 'http://localhost')
      if (url.pathname === '/api/projects/garden/shelf') return Response.json([live, other])
      if (/^\/api\/shelf\/\d+\/versions$/.test(url.pathname)) return Response.json([{ n: 2, size: 5, created_at: '2026-11-16T10:00:00Z' }, { n: 1, size: 4, created_at: '2026-11-16T09:00:00Z' }])
      return new Response('text')
    })
    vi.stubGlobal('fetch', fetchMock)
    renderShelf(false)
    fireEvent.click(await screen.findByRole('button', { name: /Version/ }))
    fireEvent.click(await screen.findByRole('option', { name: /v2/ }))
    fireEvent.click(screen.getByRole('button', { name: /Other/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    fireEvent.click(screen.getByRole('button', { name: 'Copy contents' }))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(fetchMock).toHaveBeenCalledWith('/api/shelf/8/raw?', { credentials: 'include' })
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('/api/shelf/8/raw?version'), expect.anything())
  })
})
