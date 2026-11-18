import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Preview } from '../components/Preview'

const wrap = (ui: React.ReactNode) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>
const fetched: string[] = []

beforeEach(() => {
  fetched.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    fetched.push(url)
    if (url.startsWith('/api/files/raw')) return new Response('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', { headers: { 'content-type': 'text/plain' } })
    return Response.json([])
  }))
  URL.createObjectURL = vi.fn(() => 'blob:http://localhost/abc')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

describe('Preview of inert media', () => {
  it('shows an SVG as an <img> from a blob, never inline', async () => {
    const { container, unmount } = render(wrap(<Preview src={{ kind: 'file', volume: 'work', path: 'garden/logo.svg' }} />))
    const img = await waitFor(() => { const i = container.querySelector('img'); expect(i).not.toBeNull(); return i! })
    expect(img.getAttribute('src')).toMatch(/^blob:/)
    expect(container.querySelector('svg, script')).toBeNull()
    expect(fetched.some((u) => u.includes('preview-url'))).toBe(false)
    unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:http://localhost/abc')
  })

  it('plays video, audio and images straight from the raw route, with no preview-url request', async () => {
    const { container } = render(wrap(<>
      <Preview src={{ kind: 'file', volume: 'work', path: 'garden/a.mp4' }} as="video" />
      <Preview src={{ kind: 'file', volume: 'work', path: 'garden/a.mp3' }} />
      <Preview src={{ kind: 'shelf', id: 4, path: 'a.png' }} />
    </>))
    expect(container.querySelector('video')!.getAttribute('src')).toBe('/api/files/raw?volume=work&path=garden%2Fa.mp4')
    expect(container.querySelector('audio')!.getAttribute('src')).toBe('/api/files/raw?volume=work&path=garden%2Fa.mp3')
    expect(container.querySelector('img')!.getAttribute('src')).toBe('/api/shelf/4/raw?')
    await new Promise((r) => setTimeout(r, 20))
    expect(fetched.some((u) => u.includes('preview-url'))).toBe(false)
    expect(screen.queryByText(/Loading/)).toBeNull()
  })
})
