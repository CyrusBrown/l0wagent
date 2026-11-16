import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { S } from '../api/client'
import { ChatCard } from '../screens/Chat'

let posts: { path: string; body: unknown }[] = []

beforeEach(() => {
  posts = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : null
    const url = new URL(req ? req.url : String(input), 'http://localhost')
    const method = (req?.method ?? init?.method ?? 'GET').toUpperCase()
    if (method === 'POST') {
      const text = req ? await req.clone().text() : String(init?.body ?? '')
      posts.push({ path: url.pathname, body: text ? JSON.parse(text) : null })
    }
    return Response.json({})
  }))
})
afterEach(() => vi.unstubAllGlobals())

const base = {
  kind: 'integration', preset: 'github', name: 'github', title: 'GitHub', url: 'https://api.githubcopilot.com/mcp/', secret_name: 'GITHUB_TOKEN',
  guidance: 'Create a fine-grained token with Read access.', token_url: 'https://github.com/settings/personal-access-tokens/new', reason: '',
}
const msg = (data: Record<string, unknown>) => ({ id: 7, chat_id: 1, role: 'card', body: '', data, created_at: '2026-11-16T10:00:00Z' }) as unknown as S['ChatMessage']

function show(data: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><ChatCard chatId={1} m={msg(data)} /></QueryClientProvider>)
}

describe('the chat integration card', () => {
  it('shows the steps and sends the token with Connect', async () => {
    show({ ...base, state: 'open' })
    expect(screen.getByText(/Create a fine-grained token/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /token page/ })).toHaveAttribute('href', base.token_url)
    expect(screen.getByText('Leave empty to use the saved GITHUB_TOKEN')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Token for GitHub'), { target: { value: 'ghp_abc123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].path).toBe('/api/chats/1/cards/7')
    expect(posts[0].body).toEqual({ decision: 'approve', value: 'ghp_abc123' })
  })

  it('shows the result after the decision', () => {
    show({ ...base, state: 'done', status: 'ok', tool_count: 12, last_error: null })
    expect(screen.getByText(/Connected GitHub · 12 tools/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect' })).toBeNull()
  })

  it('shows the error when the service refused', () => {
    show({ ...base, state: 'done', status: 'error', tool_count: 0, last_error: 'HTTP 401' })
    expect(screen.getByText(/answered: HTTP 401/)).toBeInTheDocument()
  })
})
