import { fireEvent, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useEscape } from '../components/useEscape'

describe('useEscape', () => {
  it('closes on Escape while open', () => {
    const onClose = vi.fn()
    renderHook(() => useEscape(true, onClose))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores other keys and does nothing while closed', () => {
    const onClose = vi.fn()
    const { rerender } = renderHook(({ open }) => useEscape(open, onClose), { initialProps: { open: false } })
    fireEvent.keyDown(window, { key: 'Escape' })
    rerender({ open: true })
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(onClose).not.toHaveBeenCalled()
    rerender({ open: false })
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('leaves an Escape already handled by a nested control alone', () => {
    const onClose = vi.fn()
    renderHook(() => useEscape(true, onClose))
    const e = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    e.preventDefault()
    window.dispatchEvent(e)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes only the most recently opened overlay per Escape', () => {
    const order: string[] = []
    const a = renderHook(() => useEscape(true, () => order.push('a')))
    const b = renderHook(() => useEscape(true, () => order.push('b')))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(order).toEqual(['b'])
    b.unmount()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(order).toEqual(['b', 'a'])
    a.unmount()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(order).toEqual(['b', 'a'])
  })
})
