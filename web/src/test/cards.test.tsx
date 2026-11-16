import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { S } from '../api/client'
import { PromoteCard, SecurityCard, SuggestionCard, TaskCard } from '../components/Cards'

const security: S['SecurityAlert'] = {
  id: 1,
  session_id: 'session-1',
  text: 'A new device signed in',
  status: 'open',
  created_at: '2026-11-16T10:00:00Z',
}

const task: S['Task'] = {
  id: 2,
  thread_id: null,
  run_id: null,
  text: 'Check the receipts',
  detail: null,
  status: 'open',
  note: null,
  created_at: '2026-11-16T10:00:00Z',
  resolved_at: null,
}

const suggestion: S['Suggestion'] = {
  id: 3,
  run_id: null,
  kind: 'thread_archive',
  target_kind: 'thread',
  target_id: 10,
  text: 'Archive the old thread',
  payload: {},
  status: 'open',
  created_at: '2026-11-16T10:00:00Z',
  decided_at: null,
}

const promote: S['PromoteRequest'] = {
  id: 4,
  run_id: 10,
  thread_id: null,
  reason: 'The reply needs more work',
  budget: 20,
  status: 'open',
  note: null,
  created_at: '2026-11-16T10:00:00Z',
  decided_at: null,
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
}

afterEach(() => {
  setOnline(true)
  vi.unstubAllGlobals()
})

function renderCards() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <SecurityCard a={security} />
      <TaskCard t={task} />
      <SuggestionCard s={suggestion} />
      <PromoteCard p={promote} />
    </QueryClientProvider>,
  )
}

describe('Card notes', () => {
  it('hides the comment control on security alerts but keeps their actions', () => {
    renderCards()
    expect(screen.getAllByText('+ Add comment')).toHaveLength(2) // the task and the promote request
    expect(screen.getByText('Check the receipts').closest('.card')).toHaveTextContent('+ Add comment')
    expect(screen.getByText('A new device signed in').closest('.card')).not.toHaveTextContent('+ Add comment')
    expect(screen.getByRole('button', { name: 'That was me' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeInTheDocument()
  })

  it('hides the comment control on suggestions', () => {
    renderCards()
    expect(screen.getByText('Archive the old thread').closest('.card')).not.toHaveTextContent('+ Add comment')
    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })
})

describe('Card decisions', () => {
  it('disables suggestion, security and promote actions while offline', () => {
    setOnline(false)
    renderCards()
    for (const name of ['Accept', 'Dismiss', 'That was me', 'Revoke', 'Approve', 'Deny']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
  })

  it('shows failed decisions', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input)
      if (url.includes('/api/suggestions/')) return Response.json({ detail: 'Suggestion failed' }, { status: 400 })
      if (url.includes('/api/security-alerts/')) return Response.json({ detail: 'Security failed' }, { status: 400 })
      return Response.json({ detail: 'Promote failed' }, { status: 400 })
    }))
    renderCards()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    expect(await screen.findByText('Suggestion failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'That was me' }))
    expect(await screen.findByText('Security failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Promote failed')).toBeInTheDocument()
  })
})
