import { beforeEach, describe, expect, it } from 'vitest'
import { setTimeFormat } from '../app/timeFormat'
import { clock, clockIn, clockSeconds, stamp, until } from '../components/format'

const at = (h: number, m = 5, s = 0) => {
  const d = new Date()
  d.setHours(h, m, s, 0)
  return d
}
const iso = (h: number, m = 5, s = 0) => at(h, m, s).toISOString()

beforeEach(() => setTimeFormat('24h'))

describe('clock', () => {
  it('renders 24-hour time by default', () => {
    expect(clock(iso(23))).toBe('23:05')
    expect(clock(iso(9))).toBe('09:05')
    expect(clock(iso(0))).toBe('00:05')
    expect(clock(iso(12))).toBe('12:05')
  })

  it('renders 12-hour time when selected', () => {
    setTimeFormat('12h')
    expect(clock(iso(23))).toBe('11:05 PM')
    expect(clock(iso(9))).toBe('9:05 AM')
    expect(clock(iso(0))).toBe('12:05 AM')
    expect(clock(iso(12))).toBe('12:05 PM')
  })

  it('can include seconds', () => {
    expect(clockSeconds(iso(23, 5, 7))).toBe('23:05:07')
    setTimeFormat('12h')
    expect(clockSeconds(iso(23, 5, 7))).toBe('11:05:07 PM')
  })

  it('renders a zoned clock time in both formats', () => {
    const d = '2026-11-16T23:05:00Z'
    expect(clockIn(d, 'UTC')).toBe('23:05')
    setTimeFormat('12h')
    expect(clockIn(d, 'UTC')).toBe('11:05 PM')
  })
})

describe('stamp', () => {
  it('stamps today in 24-hour time', () => {
    expect(stamp(iso(23))).toBe('Today 23:05')
  })

  it('stamps today in 12-hour time', () => {
    setTimeFormat('12h')
    expect(stamp(iso(23))).toBe('Today 11:05 PM')
    expect(stamp(iso(9))).toBe('Today 9:05 AM')
  })

  it('stamps older days with a date and the chosen format', () => {
    const d = new Date()
    d.setDate(d.getDate() - 10)
    d.setHours(23, 5, 0, 0)
    const day = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    expect(stamp(d.toISOString())).toBe(`${day} 23:05`)
    setTimeFormat('12h')
    expect(stamp(d.toISOString())).toBe(`${day} 11:05 PM`)
  })
})

describe('until', () => {
  it('keeps relative times', () => {
    const now = Date.now()
    expect(until(new Date(now + 2 * 3600_000).toISOString(), now)).toBe('in 2 hours')
  })

  it('shows a weekday and time beyond a day in both formats', () => {
    const now = new Date()
    const d = new Date(now.getTime() + 3 * 86400_000)
    d.setHours(23, 5, 0, 0)
    const day = d.toLocaleDateString(undefined, { weekday: 'short' })
    expect(until(d.toISOString(), now.getTime())).toBe(`${day} 23:05`)
    setTimeFormat('12h')
    expect(until(d.toISOString(), now.getTime())).toBe(`${day} 11:05 PM`)
  })
})
