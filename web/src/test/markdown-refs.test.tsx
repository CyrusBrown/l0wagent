import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it } from 'vitest'
import { Markdown, refLabel } from '../components/Markdown'

function show(md: string) {
  const router = createMemoryRouter([{ path: '*', element: <Markdown>{md}</Markdown> }])
  return render(<RouterProvider router={router} />)
}

describe('bare l0wagent:// references', () => {
  it('become links named for what they point at, without the trailing punctuation', () => {
    show('Should I post this into l0wagent://thread/20 so the assistant has it? See also l0wagent://run/7.')
    const t = screen.getByRole('link', { name: 'thread #20' })
    expect(t.getAttribute('href')).toBe('/t/20')
    expect(screen.getByRole('link', { name: 'run #7' }).getAttribute('href')).toBe('/runs/7')
    expect(screen.getByText(/so the assistant has it\?/)).toBeTruthy()
  })

  it('leave code and existing links alone', () => {
    const { container } = show('Run `l0wagent://thread/3` or [the plants](l0wagent://thread/4).')
    expect(container.querySelector('code')?.textContent).toBe('l0wagent://thread/3')
    expect(screen.getByRole('link', { name: 'the plants' }).getAttribute('href')).toBe('/t/4')
    expect(screen.getAllByRole('link')).toHaveLength(1)
  })

  it('labels', () => {
    expect(refLabel('l0wagent://thread/20#m5')).toBe('thread #20')
    expect(refLabel('l0wagent://file/work/notes/plan%201.md')).toBe('plan 1.md')
    expect(refLabel('l0wagent://project/garden')).toBe('project garden')
  })
})
