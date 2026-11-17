import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { discardItem, enqueue, flushOutbox, retryItem, type OutboxItem } from '../api/offline'
import { useOutbox } from '../api/offline'
import { renderHook } from '@testing-library/react'

const msg = (n: number): OutboxItem => ({ kind: 'message', thread: n, body: { body: `m${n}`, upload_ids: [], idempotency_key: `k${n}` }, at: '' })
const res = (status: number, body: unknown = {}, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers })
const queue = () => renderHook(() => useOutbox()).result.current

let qc: QueryClient
beforeEach(async () => {
  qc = new QueryClient()
  vi.useFakeTimers({ toFake: ['setTimeout'] })
  // empty whatever an earlier test left behind
  vi.stubGlobal('fetch', vi.fn(async () => res(200)))
  await flushOutbox(qc)
  for (const i of [...queue()]) await discardItem(i.id!)
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

const mockFetch = (...rs: Response[]) => {
  const f = vi.fn()
  rs.forEach((r) => f.mockResolvedValueOnce(r))
  f.mockResolvedValue(res(200))
  vi.stubGlobal('fetch', f)
  return f
}

describe('flushOutbox', () => {
  it('removes items on 2xx', async () => {
    await enqueue(msg(1)); await enqueue(msg(2))
    const f = mockFetch(res(201), res(200))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(2)
    expect(queue()).toHaveLength(0)
  })

  it('401 stops, keeps the queue and drops the read cache', async () => {
    await enqueue(msg(1)); await enqueue(msg(2))
    qc.setQueryData(['thread', 1], { secret: true })
    const f = mockFetch(res(401, { error: 'unauthorized' }))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(1)
    expect(queue()).toHaveLength(2)
    expect(qc.getQueryData(['thread', 1])).toBeUndefined()
  })

  it('429 stops and retries after Retry-After', async () => {
    await enqueue(msg(1)); await enqueue(msg(2))
    const f = mockFetch(res(429, {}, { 'Retry-After': '5' }))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(1)
    expect(queue()).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(4000)
    expect(f).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1500)
    expect(f).toHaveBeenCalledTimes(3)
    expect(queue()).toHaveLength(0)
  })

  it('5xx and network errors stop and keep everything', async () => {
    await enqueue(msg(1)); await enqueue(msg(2))
    const f = mockFetch(res(503))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(1)
    f.mockReset(); f.mockRejectedValue(new TypeError('offline'))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(1)
    expect(queue()).toHaveLength(2)
    expect(queue().some((i) => i.error)).toBe(false)
  })

  it('other 4xx keeps the item with its error, skips it, and goes on', async () => {
    await enqueue(msg(1)); await enqueue(msg(2))
    const f = mockFetch(res(409, { detail: 'already answered' }))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(2)
    expect(queue()).toHaveLength(1)
    expect(queue()[0].error).toBe('already answered')
    await enqueue(msg(3))
    await flushOutbox(qc)
    expect(f).toHaveBeenCalledTimes(3)  // only the new item; the failed one is skipped
    expect(queue()).toHaveLength(1)
  })

  it('retry puts a failed item back in line; discard removes it', async () => {
    await enqueue(msg(1))
    mockFetch(res(422, { error: 'bad_thing' }))
    await flushOutbox(qc)
    const id = queue()[0].id!
    expect(queue()[0].error).toBe('bad thing')
    const f = mockFetch(res(200))
    await retryItem(id, qc)
    expect(f).toHaveBeenCalledTimes(1)
    expect(queue()).toHaveLength(0)
    await enqueue(msg(2))
    mockFetch(res(400))
    await flushOutbox(qc)
    await discardItem(queue()[0].id!)
    expect(queue()).toHaveLength(0)
  })
})
