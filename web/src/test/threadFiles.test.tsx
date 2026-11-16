import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { THREAD_FILES_DEFAULT, THREAD_FILES_KEY, THREAD_FILES_MAX, THREAD_FILES_MIN, ThreadFiles } from '../components/ThreadFiles'

vi.mock('../components/ShelfView', () => ({ ShelfView: () => <div data-testid="shelf" /> }))

const open = () => render(<ThreadFiles project="garden" onClose={vi.fn()} onLink={vi.fn()} />)
const handle = () => screen.getByRole('separator', { name: 'Resize project files' })
const width = () => Number(handle().getAttribute('aria-valuenow'))

describe('ThreadFiles', () => {
  beforeEach(() => localStorage.clear())

  it('starts at the default width', () => {
    open()
    expect(width()).toBe(THREAD_FILES_DEFAULT)
  })

  it('resizes with the keyboard and persists the width', () => {
    const first = open()
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(width()).toBe(THREAD_FILES_DEFAULT + 16)
    fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(width()).toBe(THREAD_FILES_DEFAULT)
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(localStorage.getItem(THREAD_FILES_KEY)).toBe(String(THREAD_FILES_DEFAULT + 32))
    first.unmount()

    open()
    expect(width()).toBe(THREAD_FILES_DEFAULT + 32)
  })

  it('clamps the width to the allowed range', () => {
    open()
    for (let i = 0; i < 30; i++) fireEvent.keyDown(handle(), { key: 'ArrowLeft' })
    expect(width()).toBe(THREAD_FILES_MAX)
    for (let i = 0; i < 40; i++) fireEvent.keyDown(handle(), { key: 'ArrowRight' })
    expect(width()).toBe(THREAD_FILES_MIN)
    expect(localStorage.getItem(THREAD_FILES_KEY)).toBe(String(THREAD_FILES_MIN))
  })

  it('jumps to the ends with Home and End', () => {
    open()
    fireEvent.keyDown(handle(), { key: 'Home' })
    expect(width()).toBe(THREAD_FILES_MIN)
    fireEvent.keyDown(handle(), { key: 'End' })
    expect(width()).toBe(THREAD_FILES_MAX)
  })

  it('reads a stored width and ignores bad or out-of-range values', () => {
    localStorage.setItem(THREAD_FILES_KEY, '420')
    const first = open()
    expect(width()).toBe(420)
    first.unmount()

    localStorage.setItem(THREAD_FILES_KEY, '9000')
    const second = open()
    expect(width()).toBe(THREAD_FILES_MAX)
    second.unmount()

    localStorage.setItem(THREAD_FILES_KEY, 'wide')
    open()
    expect(width()).toBe(THREAD_FILES_DEFAULT)
  })

  it('exposes an accessible vertical separator', () => {
    open()
    expect(handle()).toHaveAttribute('aria-orientation', 'vertical')
    expect(handle()).toHaveAttribute('aria-valuemin', String(THREAD_FILES_MIN))
    expect(handle()).toHaveAttribute('aria-valuemax', String(THREAD_FILES_MAX))
    expect(handle()).toHaveAttribute('aria-controls', 'thread-files-panel')
    expect(handle()).toHaveAttribute('tabindex', '0')
  })
})
