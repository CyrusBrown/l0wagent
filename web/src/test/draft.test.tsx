import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { useDraft } from '../components/useDraft'

describe('useDraft', () => {
  afterEach(() => localStorage.clear())

  it('keeps an unsent draft across remounts, per key, and forgets it when cleared', () => {
    const a = renderHook(() => useDraft('new:general'))
    act(() => a.result.current[1]('half a prompt'))
    act(() => a.result.current[1]((t) => t + ' and more'))
    a.unmount()
    expect(renderHook(() => useDraft('new:general')).result.current[0]).toBe('half a prompt and more')
    expect(renderHook(() => useDraft('new:garden')).result.current[0]).toBe('')
    const b = renderHook(() => useDraft('new:general'))
    act(() => b.result.current[2]())
    b.unmount()
    expect(renderHook(() => useDraft('new:general')).result.current[0]).toBe('')
  })

  it('switches to the other key’s draft when the key changes', () => {
    localStorage.setItem('l0w-draft:thread:2', 'reply for two')
    const h = renderHook(({ k }) => useDraft(k), { initialProps: { k: 'thread:1' } })
    act(() => h.result.current[1]('reply for one'))
    h.rerender({ k: 'thread:2' })
    expect(h.result.current[0]).toBe('reply for two')
    expect(localStorage.getItem('l0w-draft:thread:1')).toBe('reply for one')
  })
})
