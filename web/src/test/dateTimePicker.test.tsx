import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { DatePicker, TimePicker } from '../components/DateTimePicker'
import { useEscape } from '../components/useEscape'

const dateText = (iso: string) => new Date(Number(iso.slice(0, 4)), Number(iso.slice(5, 7)) - 1, Number(iso.slice(8, 10)))
  .toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
const monthText = (y: number, m: number) => new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

function renderDate(value = '2026-11-16') {
  const onChange = vi.fn()
  render(<DatePicker label="Date" value={value} onChange={onChange} />)
  const trigger = screen.getByRole('button', { name: `Date: ${dateText(value)}` })
  return { onChange, trigger }
}

describe('DatePicker', () => {
  it('opens a month grid from a labelled trigger and focuses into it', () => {
    const { trigger } = renderDate()
    fireEvent.click(trigger)
    const grid = screen.getByRole('grid', { name: 'Date calendar' })
    const popup = screen.getByRole('dialog', { name: 'Date' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(trigger).toHaveAttribute('aria-controls', popup.id)
    expect(grid).toHaveFocus()
    expect(screen.getByText(monthText(2026, 11))).toBeInTheDocument()
    expect(screen.getByRole('gridcell', { name: '2026-11-16' })).toHaveAttribute('aria-selected', 'true')
  })

  it('chooses a date, closes and returns focus to the trigger', async () => {
    const { onChange, trigger } = renderDate()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('gridcell', { name: '2026-11-20' }))
    expect(onChange).toHaveBeenCalledWith('2026-11-20')
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('navigates months with previous and next', () => {
    const { onChange, trigger } = renderDate()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: 'Next month' }))
    expect(screen.getByText(monthText(2026, 12))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    expect(screen.getByText(monthText(2026, 11))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next month' }))
    fireEvent.click(screen.getByRole('gridcell', { name: '2026-12-03' }))
    expect(onChange).toHaveBeenCalledWith('2026-12-03')
  })

  it('selects by keyboard with the arrow keys and Enter', async () => {
    const { onChange, trigger } = renderDate()
    fireEvent.click(trigger)
    const grid = screen.getByRole('grid', { name: 'Date calendar' })
    fireEvent.keyDown(grid, { key: 'ArrowRight' })
    fireEvent.keyDown(grid, { key: 'ArrowDown' })
    fireEvent.keyDown(grid, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('2026-11-24')
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
  })

  it('closes on Escape without choosing', async () => {
    const { onChange, trigger } = renderDate()
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('grid'), { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('closes only the picker when nested in a dialog', async () => {
    function Harness() {
      const [open, setOpen] = useState(true)
      useEscape(open, () => setOpen(false))
      return open
        ? <div role="dialog" aria-label="Parent"><DatePicker label="Date" value="2026-11-16" onChange={vi.fn()} /></div>
        : <div>closed</div>
    }
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: `Date: ${dateText('2026-11-16')}` }))
    fireEvent.keyDown(screen.getByRole('grid'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Parent' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Parent' })).not.toBeInTheDocument())
  })

  it('does not open when disabled', () => {
    render(<DatePicker label="Date" value="2026-11-16" onChange={vi.fn()} disabled />)
    fireEvent.click(screen.getByRole('button', { name: `Date: ${dateText('2026-11-16')}` }))
    expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  })
})

describe('TimePicker', () => {
  it('selects an hour and minute with the keyboard', async () => {
    const onChange = vi.fn()
    render(<TimePicker label="Time" value="09:30" onChange={onChange} />)
    const trigger = screen.getByRole('button', { name: 'Time: 09:30' })
    fireEvent.click(trigger)
    const hours = screen.getByRole('listbox', { name: 'Time: hours' })
    const minutes = screen.getByRole('listbox', { name: 'Time: minutes' })
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(hours).toHaveFocus()
    fireEvent.keyDown(hours, { key: 'ArrowDown' })
    fireEvent.keyDown(hours, { key: 'ArrowRight' })
    expect(minutes).toHaveFocus()
    fireEvent.keyDown(minutes, { key: 'ArrowDown' })
    fireEvent.keyDown(minutes, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('10:31')
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('picks hours and minutes by click and commits with Set', () => {
    const onChange = vi.fn()
    render(<TimePicker label="Time" value="09:30" onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Time: 09:30' }))
    fireEvent.click(within(screen.getByRole('listbox', { name: 'Time: hours' })).getByRole('option', { name: '23' }))
    fireEvent.click(within(screen.getByRole('listbox', { name: 'Time: minutes' })).getByRole('option', { name: '05' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set' }))
    expect(onChange).toHaveBeenCalledWith('23:05')
  })

  it('closes on Escape without choosing, and leaves a parent dialog open', async () => {
    const onChange = vi.fn()
    function Harness() {
      const [open, setOpen] = useState(true)
      useEscape(open, () => setOpen(false))
      return open
        ? <div role="dialog" aria-label="Parent"><TimePicker label="Time" value="09:30" onChange={onChange} /></div>
        : <div>closed</div>
    }
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Time: 09:30' }))
    fireEvent.keyDown(screen.getByRole('listbox', { name: 'Time: hours' }), { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'Parent' })).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Parent' })).not.toBeInTheDocument())
  })

  it('clears an empty-capable value', () => {
    const onChange = vi.fn()
    render(<TimePicker label="Quiet from" value="22:15" onChange={onChange} clearable />)
    fireEvent.click(screen.getByRole('button', { name: 'Quiet from: 22:15' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('shows a 12-hour label and commits the 24-hour value', () => {
    const onChange = vi.fn()
    render(<TimePicker label="Time" value="13:05" onChange={onChange} hour12 />)
    fireEvent.click(screen.getByRole('button', { name: 'Time: 1:05 PM' }))
    const hours = screen.getByRole('listbox', { name: 'Time: hours' })
    expect(screen.getByRole('option', { name: '1' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(within(hours).getByRole('option', { name: '7' }))
    fireEvent.click(screen.getByRole('button', { name: 'AM' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set' }))
    expect(onChange).toHaveBeenCalledWith('07:05')
  })
})
