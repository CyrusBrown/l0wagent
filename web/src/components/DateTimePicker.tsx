import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { usePresence } from './motion'
import './DateTimePicker.css'

const pad = (n: number) => String(n).padStart(2, '0')
const isoOf = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
const monthOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), 1)
const addDays = (d: Date, n: number) => { const x = new Date(d); x.setDate(d.getDate() + n); return x }
const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

function parseIso(s: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  if (!m) return null
  const y = Number(m[1]), mo = Number(m[2]), d = Number(m[3])
  const date = new Date(y, mo - 1, d)
  return date.getFullYear() === y && date.getMonth() === mo - 1 && date.getDate() === d ? date : null
}

function shiftMonth(d: Date, delta: number): Date {
  const month = new Date(d.getFullYear(), d.getMonth() + delta, 1)
  const last = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  return new Date(month.getFullYear(), month.getMonth(), Math.min(d.getDate(), last))
}

const dateText = (d: Date) => d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
const monthText = (d: Date) => d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

type Pos = { left: number; top?: number; bottom?: number; width: number }

function place(btn: HTMLElement | null, width: number, height: number): Pos | null {
  if (!btn) return null
  const r = btn.getBoundingClientRect()
  const up = r.bottom + height + 8 > window.innerHeight && r.top > height + 8
  return {
    left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)),
    width,
    ...(up ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }),
  }
}

export interface DatePickerProps {
  value: string
  onChange: (value: string) => void
  label: string
  disabled?: boolean
  className?: string
}

export function DatePicker({ value, onChange, label, disabled, className }: DatePickerProps) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<Pos | null>(null)
  const parsed = parseIso(value)
  const today = startOfDay(new Date())
  const [view, setView] = useState<Date>(() => monthOf(parsed ?? new Date()))
  const [active, setActive] = useState<Date>(() => parsed ?? today)
  const btn = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  const grid = useRef<HTMLDivElement>(null)
  const { mounted, closing } = usePresence(open, 140)
  const id = useId()
  const popId = `${id}-pop`
  const cellId = (d: Date) => `${id}-d-${isoOf(d)}`

  const close = (refocus = true) => { setOpen(false); if (refocus) btn.current?.focus() }
  const closeRef = useRef(close)
  closeRef.current = close

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      e.stopPropagation()
      closeRef.current()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open])

  useEffect(() => { if (open && mounted) grid.current?.focus() }, [open, mounted])

  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => { if (!pop.current?.contains(e.target as Node)) close(false) }
    const onResize = () => close(false)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('scroll', onScroll, true); window.removeEventListener('resize', onResize) }
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps

  const show = () => {
    if (disabled) return
    const d = parsed ?? today
    setView(monthOf(d))
    setActive(d)
    setPos(place(btn.current, 264, 340))
    setOpen(true)
  }

  const choose = (d: Date) => {
    const next = isoOf(d)
    close()
    if (next !== value) onChange(next)
  }

  const move = (days: number) => {
    const next = addDays(active, days)
    setActive(next)
    if (next.getMonth() !== view.getMonth() || next.getFullYear() !== view.getFullYear()) setView(monthOf(next))
  }

  const go = (delta: number) => {
    const next = shiftMonth(active, delta)
    setActive(next)
    setView(monthOf(next))
  }

  const onTriggerKey = (e: ReactKeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); show() }
      return
    }
    if (e.key === 'Escape') { e.preventDefault(); close() }
  }

  const onGridKey = (e: ReactKeyboardEvent) => {
    if (e.key === 'ArrowLeft') { e.preventDefault(); move(-1) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); move(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-7) }
    else if (e.key === 'ArrowDown') { e.preventDefault(); move(7) }
    else if (e.key === 'PageUp') { e.preventDefault(); go(-1) }
    else if (e.key === 'PageDown') { e.preventDefault(); go(1) }
    else if (e.key === 'Home') { e.preventDefault(); move(-((active.getDay() + 6) % 7)) }
    else if (e.key === 'End') { e.preventDefault(); move(6 - ((active.getDay() + 6) % 7)) }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(active) }
  }

  const onPopKey = (e: ReactKeyboardEvent) => {
    if (e.defaultPrevented) return
    if (e.key === 'Escape') { e.preventDefault(); close() }
    else if (e.key === 'Tab') close(false)
  }

  const first = monthOf(view)
  const lead = (first.getDay() + 6) % 7
  const start = addDays(first, -lead)
  const cells = Array.from({ length: 42 }, (_, i) => addDays(start, i))

  return <>
    <button ref={btn} type="button" className={`dtp-trigger dtp-date ${open ? 'open' : ''} ${className ?? ''}`}
      aria-label={`${label}: ${parsed ? dateText(parsed) : 'not set'}`} aria-haspopup="dialog" aria-expanded={open}
      aria-controls={open ? popId : undefined} disabled={disabled}
      onClick={() => (open ? close() : show())} onKeyDown={onTriggerKey}>
      <span className="dtp-value">{parsed ? dateText(parsed) : <span className="dtp-placeholder">Choose date</span>}</span>
    </button>
    {mounted && pos && createPortal(
      <div className="dtp-scrim" onMouseDown={() => close(false)}>
        <div ref={pop} id={popId} className={`dtp-pop dtp-pop-date ${closing ? 'closing' : ''}`} role="dialog" aria-label={label}
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom, width: pos.width }}
          onMouseDown={(e) => e.stopPropagation()} onKeyDown={onPopKey}>
          <div className="dtp-head">
            <button type="button" className="dtp-nav" aria-label="Previous month" onClick={() => go(-1)}>‹</button>
            <div className="dtp-title" aria-live="polite">{monthText(view)}</div>
            <button type="button" className="dtp-nav" aria-label="Next month" onClick={() => go(1)}>›</button>
          </div>
          <div className="dtp-week">{DOW.map((d) => <span key={d}>{d}</span>)}</div>
          <div ref={grid} className="dtp-grid" role="grid" aria-label={`${label} calendar`} tabIndex={-1}
            aria-activedescendant={cellId(active)} onKeyDown={onGridKey}>
            {Array.from({ length: 6 }, (_, row) => (
              <div key={row} role="row" className="dtp-row">
                {cells.slice(row * 7, row * 7 + 7).map((d) => {
                  const iso = isoOf(d)
                  return (
                    <div key={iso} id={cellId(d)} role="gridcell" aria-label={iso}
                      aria-selected={iso === value} aria-current={sameDay(d, today) ? 'date' : undefined}
                      className={`dtp-cell ${d.getMonth() !== view.getMonth() ? 'out' : ''} ${iso === value ? 'sel' : ''} ${sameDay(d, today) ? 'today' : ''} ${sameDay(d, active) ? 'act' : ''}`}
                      onClick={() => choose(d)}>{d.getDate()}</div>
                  )
                })}
              </div>
            ))}
          </div>
          <div className="dtp-foot"><button type="button" className="dtp-link" onClick={() => choose(today)}>Today</button></div>
        </div>
      </div>,
      document.body,
    )}
  </>
}

export interface TimePickerProps {
  value: string
  onChange: (value: string) => void
  label: string
  disabled?: boolean
  className?: string
  hour12?: boolean
  clearable?: boolean
}

function parseTime(s: string): { h: number; m: number } | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(s)
  if (!match) return null
  const h = Number(match[1]), m = Number(match[2])
  return h >= 0 && h < 24 && m >= 0 && m < 60 ? { h, m } : null
}

const timeText = (t: { h: number; m: number }, hour12: boolean) => hour12
  ? `${t.h % 12 || 12}:${pad(t.m)} ${t.h < 12 ? 'AM' : 'PM'}`
  : `${pad(t.h)}:${pad(t.m)}`

export function TimePicker({ value, onChange, label, disabled, className, hour12 = false, clearable = false }: TimePickerProps) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<Pos | null>(null)
  const [hour, setHour] = useState(0)
  const [minute, setMinute] = useState(0)
  const parsed = parseTime(value)
  const btn = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  const hourCol = useRef<HTMLDivElement>(null)
  const minuteCol = useRef<HTMLDivElement>(null)
  const { mounted, closing } = usePresence(open, 140)
  const id = useId()
  const popId = `${id}-pop`

  const close = (refocus = true) => { setOpen(false); if (refocus) btn.current?.focus() }
  const closeRef = useRef(close)
  closeRef.current = close

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      e.stopPropagation()
      closeRef.current()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open])

  useEffect(() => { if (open && mounted) hourCol.current?.focus() }, [open, mounted])

  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => { if (!pop.current?.contains(e.target as Node)) close(false) }
    const onResize = () => close(false)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('scroll', onScroll, true); window.removeEventListener('resize', onResize) }
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps

  const hours = hour12 ? Array.from({ length: 12 }, (_, i) => i + 1) : Array.from({ length: 24 }, (_, i) => i)
  const selHour = hour12 ? (hour % 12 || 12) : hour

  useEffect(() => {
    if (!open) return
    hourCol.current?.querySelector<HTMLElement>(`[data-v="${selHour}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [open, selHour])

  useEffect(() => {
    if (!open) return
    minuteCol.current?.querySelector<HTMLElement>(`[data-v="${minute}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [open, minute])

  const show = () => {
    if (disabled) return
    const now = new Date()
    const t = parsed ?? { h: now.getHours(), m: now.getMinutes() }
    setHour(t.h)
    setMinute(t.m)
    setPos(place(btn.current, 190, 300))
    setOpen(true)
  }

  const commit = () => {
    const next = `${pad(hour)}:${pad(minute)}`
    close()
    if (next !== value) onChange(next)
  }

  const clear = () => { close(); onChange('') }

  const pickHour = (v: number) => {
    setHour(hour12 ? (v % 12) + (hour >= 12 ? 12 : 0) : v)
  }

  const stepHour = (dir: number) => {
    const i = hours.indexOf(selHour)
    pickHour(hours[Math.max(0, Math.min(hours.length - 1, i + dir))])
  }

  const stepMinute = (dir: number) => setMinute(Math.max(0, Math.min(59, minute + dir)))

  const onTriggerKey = (e: ReactKeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); show() }
      return
    }
    if (e.key === 'Escape') { e.preventDefault(); close() }
  }

  const onHourKey = (e: ReactKeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); stepHour(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); stepHour(-1) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); minuteCol.current?.focus() }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); hourCol.current?.focus() }
    else if (e.key === 'Home') { e.preventDefault(); pickHour(hours[0]) }
    else if (e.key === 'End') { e.preventDefault(); pickHour(hours[hours.length - 1]) }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); commit() }
  }

  const onMinuteKey = (e: ReactKeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); stepMinute(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); stepMinute(-1) }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); hourCol.current?.focus() }
    else if (e.key === 'ArrowRight') { e.preventDefault(); minuteCol.current?.focus() }
    else if (e.key === 'Home') { e.preventDefault(); setMinute(0) }
    else if (e.key === 'End') { e.preventDefault(); setMinute(59) }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); commit() }
  }

  const onPopKey = (e: ReactKeyboardEvent) => {
    if (e.defaultPrevented) return
    if (e.key === 'Escape') { e.preventDefault(); close() }
    else if (e.key === 'Tab') close(false)
  }

  const pm = hour >= 12

  return <>
    <button ref={btn} type="button" className={`dtp-trigger dtp-time ${open ? 'open' : ''} ${className ?? ''}`}
      aria-label={`${label}: ${parsed ? timeText(parsed, hour12) : 'not set'}`} aria-haspopup="dialog" aria-expanded={open}
      aria-controls={open ? popId : undefined} disabled={disabled}
      onClick={() => (open ? close() : show())} onKeyDown={onTriggerKey}>
      <span className="dtp-value">{parsed ? timeText(parsed, hour12) : <span className="dtp-placeholder">Set time</span>}</span>
    </button>
    {mounted && pos && createPortal(
      <div className="dtp-scrim" onMouseDown={() => close(false)}>
        <div ref={pop} id={popId} className={`dtp-pop dtp-pop-time ${closing ? 'closing' : ''}`} role="dialog" aria-label={label}
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom, width: pos.width }}
          onMouseDown={(e) => e.stopPropagation()} onKeyDown={onPopKey}>
          <div className="dtp-time-cols">
            <div className="dtp-col-wrap">
              <div className="dtp-col-head">Hour</div>
              <div ref={hourCol} className="dtp-col" role="listbox" aria-label={`${label}: hours`} tabIndex={-1}
                aria-activedescendant={`${id}-h-${selHour}`} onKeyDown={onHourKey}>
                {hours.map((v) => (
                  <div key={v} id={`${id}-h-${v}`} data-v={v} role="option" aria-selected={v === selHour}
                    className={`dtp-opt ${v === selHour ? 'on' : ''}`}
                    onClick={() => { pickHour(v); hourCol.current?.focus() }}>{hour12 ? v : pad(v)}</div>
                ))}
              </div>
            </div>
            <div className="dtp-col-wrap">
              <div className="dtp-col-head">Minute</div>
              <div ref={minuteCol} className="dtp-col" role="listbox" aria-label={`${label}: minutes`} tabIndex={-1}
                aria-activedescendant={`${id}-m-${minute}`} onKeyDown={onMinuteKey}>
                {Array.from({ length: 60 }, (_, v) => (
                  <div key={v} id={`${id}-m-${v}`} data-v={v} role="option" aria-selected={v === minute}
                    className={`dtp-opt ${v === minute ? 'on' : ''}`}
                    onClick={() => { setMinute(v); minuteCol.current?.focus() }}>{pad(v)}</div>
                ))}
              </div>
            </div>
          </div>
          {hour12 && (
            <div className="dtp-meridiem" role="group" aria-label={`${label}: meridiem`}>
              <button type="button" className={!pm ? 'on' : ''} aria-pressed={!pm} onClick={() => setHour(hour % 12)}>AM</button>
              <button type="button" className={pm ? 'on' : ''} aria-pressed={pm} onClick={() => setHour((hour % 12) + 12)}>PM</button>
            </div>
          )}
          <div className="dtp-foot">
            {clearable && value !== '' && <button type="button" className="dtp-link" onClick={clear}>Clear</button>}
            <button type="button" className="dtp-set" onClick={commit}>Set</button>
          </div>
        </div>
      </div>,
      document.body,
    )}
  </>
}
