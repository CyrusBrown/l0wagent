import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Select } from '../components/Select'

const options = [
  { value: 'apple', label: 'Apple' },
  { value: 'blueberry', label: 'Blueberry' },
  { value: 'cherry', label: 'Cherry' },
]

beforeEach(() => { Element.prototype.scrollIntoView = vi.fn() })

describe('Select', () => {
  it('shows the current label in its accessible name', () => {
    render(<Select value="apple" options={options} onChange={vi.fn()} label="Fruit" />)
    expect(screen.getByRole('button', { name: 'Fruit: Apple' })).toHaveTextContent('Apple')
  })

  it('opens the listbox and picks an option', async () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    fireEvent.click(screen.getByRole('button', { name: 'Fruit: Apple' }))
    expect(screen.getByRole('listbox', { name: 'Fruit' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'Blueberry' }))
    expect(onChange).toHaveBeenCalledWith('blueberry')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('moves with the arrow keys and selects with Enter', () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('blueberry')
  })

  it('closes on Escape without changing and returns focus', async () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    trigger.focus()
    fireEvent.click(trigger)
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('closes on an outside click without changing', async () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    fireEvent.click(screen.getByRole('button', { name: 'Fruit: Apple' }))
    const scrim = document.querySelector('.sel-scrim')
    expect(scrim).not.toBeNull()
    fireEvent.mouseDown(scrim!)
    expect(onChange).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('closes on Tab without preventing the focus move', async () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    trigger.focus()
    fireEvent.click(trigger)
    expect(fireEvent.keyDown(trigger, { key: 'Tab' })).toBe(true)
    expect(onChange).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('renders group headings once and keeps options reachable', () => {
    const grouped = [
      { value: 'apple', label: 'Apple', group: 'Fruit' },
      { value: 'blueberry', label: 'Blueberry', group: 'Berries' },
      { value: 'cherry', label: 'Cherry', group: 'Berries' },
    ]
    render(<Select value="apple" options={grouped} onChange={vi.fn()} label="Fruit" />)
    fireEvent.click(screen.getByRole('button', { name: 'Fruit: Apple' }))
    expect(screen.getByText('Fruit')).toBeInTheDocument()
    expect(screen.getAllByText('Berries')).toHaveLength(1)
    expect(screen.getByRole('option', { name: 'Cherry' })).toBeInTheDocument()
  })

  it('wires aria-controls and aria-activedescendant', () => {
    render(<Select value="apple" options={options} onChange={vi.fn()} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    fireEvent.click(trigger)
    const listbox = screen.getByRole('listbox', { name: 'Fruit' })
    expect(trigger).toHaveAttribute('aria-controls', listbox.id)
    expect(listbox).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: /Apple/ }).id)
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(listbox).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Blueberry' }).id)
  })

  it('jumps by typed letters and keeps the key from global shortcuts', () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={options} onChange={onChange} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(fireEvent.keyDown(trigger, { key: 'c' })).toBe(false)
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('cherry')
  })

  it('does not choose a disabled option', () => {
    const onChange = vi.fn()
    render(<Select value="apple" options={[...options.slice(0, 1), { ...options[1], disabled: true }]} onChange={onChange} label="Fruit" />)
    fireEvent.click(screen.getByRole('button', { name: 'Fruit: Apple' }))
    const off = screen.getByRole('option', { name: 'Blueberry' })
    expect(off).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(off)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('moves focus into the listbox on open and back to the trigger on close', async () => {
    render(<Select value="apple" options={options} onChange={vi.fn()} label="Fruit" />)
    const trigger = screen.getByRole('button', { name: 'Fruit: Apple' })
    trigger.focus()
    fireEvent.click(trigger)
    const listbox = screen.getByRole('listbox', { name: 'Fruit' })
    await waitFor(() => expect(listbox).toHaveFocus())
    fireEvent.keyDown(listbox, { key: 'ArrowDown' })
    expect(listbox).toHaveAttribute('aria-activedescendant', screen.getByRole('option', { name: 'Blueberry' }).id)
    fireEvent.keyDown(listbox, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})
