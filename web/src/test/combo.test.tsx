import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelPicker } from '../components/settings'
import type { components } from '../api/schema'

const provider = { id: 'claude', kind: 'claude-subscription', label: 'Claude', enabled: true, status: '', usage_source: '', has_credential: true } as components['schemas']['ProviderInfo']

function setup(onChange = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ModelPicker providers={[provider]} provider="claude" tier="sonnet/medium" onChange={onChange} label="Model" />
    </QueryClientProvider>,
  )
  return { onChange, trigger: screen.getByRole('button', { name: 'Model' }) }
}

beforeEach(() => { Element.prototype.scrollIntoView = vi.fn() })

describe('Combo', () => {
  it('closes on Tab without trapping focus', async () => {
    const { trigger } = setup()
    fireEvent.click(trigger)
    const search = screen.getByRole('combobox')
    expect(fireEvent.keyDown(search, { key: 'Tab' })).toBe(true)
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('picks on click and returns focus to the trigger', async () => {
    const { trigger, onChange } = setup()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('option', { name: /Opus/ }))
    expect(onChange).toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('caps the popup height inline', () => {
    const { trigger } = setup()
    fireEvent.click(trigger)
    expect(screen.getByRole('listbox').style.maxHeight).toMatch(/^\d+px$/)
  })
})
