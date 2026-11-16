import { type ReactNode, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { usePresence } from './motion'

// The app's dropdown (owner feedback, 2026-11-17): native select lists are drawn by the OS and
// can't be styled, so every dropdown is this: a button and a listbox in the app's style, with
// the mockup's motion (pops in, sinks out), arrow keys, Enter, Esc and type-to-jump.

export interface Option<T extends string | number = string> { value: T; label: ReactNode; text?: string; hint?: string; group?: string; disabled?: boolean }

export function Select<T extends string | number>({ value, options, onChange, label, variant = 'input', disabled, className }: {
  value: T; options: Option<T>[]; onChange: (v: T) => void; label: string
  variant?: 'input' | 'big' | 'filter' | 'pick'; disabled?: boolean; className?: string
}) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number; width: number } | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const typed = useRef({ s: '', t: 0 })
  const { mounted, closing } = usePresence(open, 140)
  const id = useId()
  const listId = `${id}-listbox`
  const optId = (i: number) => `${id}-opt-${i}`
  const cur = options.find((o) => o.value === value)
  const enabled = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0)
  const text = (o: Option<T>) => o.text ?? (typeof o.label === 'string' || typeof o.label === 'number' ? String(o.label) : String(o.value))

  const show = () => {
    if (disabled) return
    const r = btn.current!.getBoundingClientRect()
    const h = Math.min(320, options.length * 36 + 12)
    const up = r.bottom + h + 8 > window.innerHeight && r.top > h + 8
    const width = Math.max(r.width, 180)
    setPos({ left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)), width, ...(up ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }) })
    setActive(Math.max(0, options.findIndex((o) => o.value === value)))
    setOpen(true)
  }
  const close = (refocus = true) => { setOpen(false); if (refocus) btn.current?.focus() }
  const choose = (i: number) => { const o = options[i]; if (!o || o.disabled) return; close(); if (o.value !== value) onChange(o.value) }
  const move = (d: number) => {
    const at = enabled.indexOf(active)
    setActive(enabled[Math.min(enabled.length - 1, Math.max(0, (at < 0 ? 0 : at) + d))] ?? active)
  }
  useEffect(() => { if (open) list.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' }) }, [active, open])
  useEffect(() => { if (open && mounted) list.current?.focus() }, [open, mounted])
  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => { if (!list.current?.contains(e.target as Node)) close(false) }
    const onResize = () => close(false)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('scroll', onScroll, true); window.removeEventListener('resize', onResize) }
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps

  const onKey = (e: React.KeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); show() }
      return
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
    else if (e.key === 'Home') { e.preventDefault(); setActive(enabled[0]) }
    else if (e.key === 'End') { e.preventDefault(); setActive(enabled[enabled.length - 1]) }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(active) }
    else if (e.key === 'Escape') { e.preventDefault(); close() }
    else if (e.key === 'Tab') { close() }
    else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault()
      const now = Date.now()
      typed.current = { s: (now - typed.current.t < 700 ? typed.current.s : '') + e.key.toLowerCase(), t: now }
      const i = options.findIndex((o) => !o.disabled && text(o).toLowerCase().startsWith(typed.current.s))
      if (i >= 0) setActive(i)
    }
  }

  let lastGroup: string | undefined
  return <>
    <button ref={btn} type="button" className={`sel-btn sel-${variant} ${open ? 'open' : ''} ${className ?? ''}`}
      aria-label={`${label}: ${cur ? text(cur) : 'Choose…'}`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? listId : undefined}
      disabled={disabled} onClick={() => (open ? close() : show())} onKeyDown={onKey}>
      <span className="sel-text">{cur ? cur.label : <span className="sel-placeholder">Choose…</span>}</span>
    </button>
    {mounted && pos && createPortal(
      <div className="sel-scrim" onMouseDown={() => close(false)}>
        <div ref={list} id={listId} className={`sel-pop ${closing ? 'closing' : ''}`} role="listbox" aria-label={label} tabIndex={-1}
          aria-activedescendant={open && options[active] ? optId(active) : undefined}
          onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom, width: pos.width }}>
          {options.map((o, i) => {
            const head = o.group && o.group !== lastGroup ? o.group : null
            lastGroup = o.group
            return (
              <div key={String(o.value)}>
                {head && <div className="sel-group">{head}</div>}
                <div id={optId(i)} data-i={i} role="option" aria-selected={o.value === value} aria-disabled={o.disabled}
                  className={`sel-opt ${o.value === value ? 'on' : ''} ${i === active ? 'act' : ''} ${o.disabled ? 'dis' : ''}`}
                  onMouseEnter={() => !o.disabled && setActive(i)} onClick={() => choose(i)}>
                  <span className="sel-check">{o.value === value ? '✓' : ''}</span>
                  <span className="stack" style={{ gap: 1, minWidth: 0 }}><span className="ellipsis">{o.label}</span>{o.hint && <span className="sub ellipsis">{o.hint}</span>}</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>,
      document.body,
    )}
  </>
}
