import { type ReactNode, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { S } from '../api/client'
import { useProviderCatalog } from '../api/queries'
import { joinTier, modelChoices, splitTier, type ModelChoice } from './models'
import { Select } from './Select'
import { useEscape } from './useEscape'

// Settings controls (owner feedback, 2026-11-17): every value is edited in place and saved when
// it changes, with a quiet "Saved" note; no chips that open editors, no Save buttons (except
// where something must be confirmed: keys, passwords).

/** A labeled setting: name and hint on the left, the control on the right; a changed value can
 * show the default it replaces and a Reset. */
export function Row({ label, hint, children, was, onReset, wide }: {
  label: string; hint?: ReactNode; children: ReactNode; was?: string; onReset?: () => void; wide?: boolean
}) {
  return (
    <div className={`set-row2 ${wide ? 'wide' : ''}`}>
      <div className="stack" style={{ gap: 2, minWidth: 0 }}>
        <span className="k">{label}{was && <span className="custom-tag">custom</span>}</span>
        {hint && <span className="sub h">{hint}</span>}
        {was && <span className="sub h">{was}{onReset && <> · <button className="link-btn" onClick={onReset}>Reset</button></>}</span>}
      </div>
      <div className="ctl">{children}</div>
    </div>
  )
}

/** "Saved" for a moment after `token` changes (pass a mutation's submittedAt, or a counter). */
export function Saved({ token, error }: { token: number; error?: string | null }) {
  const [show, setShow] = useState(false)
  useEffect(() => {
    if (!token) return
    setShow(true)
    const t = setTimeout(() => setShow(false), 1600)
    return () => clearTimeout(t)
  }, [token])
  if (error) return <span className="saved err" role="alert">{error}</span>
  return <span className={`saved ${show ? 'on' : ''}`} aria-live="polite">{show ? 'Saved' : ''}</span>
}

/** A number saved on blur or Enter (when it changed and is in range). */
export function NumberField({ value, onCommit, min = 0, max, step = 1, suffix, label, width = 76 }: {
  value: number; onCommit: (v: number) => void; min?: number; max?: number; step?: number; suffix?: string; label: string; width?: number
}) {
  const [v, setV] = useState(String(value))
  useEffect(() => setV(String(value)), [value])
  const commit = () => {
    const n = Number(v)
    if (v.trim() === '' || Number.isNaN(n) || n < min || (max != null && n > max)) { setV(String(value)); return }
    if (n !== value) onCommit(n)
  }
  return (
    <span className="num-field">
      <input className="input" type="number" min={min} max={max} step={step} value={v} style={{ width }} aria-label={label}
        onChange={(e) => setV(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
      {suffix && <span className="sub">{suffix}</span>}
    </span>
  )
}

/** Text saved on blur or Enter, if `parse` accepts it. */
export function TextField<T>({ value, format, parse, onCommit, label, placeholder, width = 220 }: {
  value: T; format: (v: T) => string; parse: (t: string) => T | null; onCommit: (v: T) => void; label: string; placeholder?: string; width?: number
}) {
  const [t, setT] = useState(format(value))
  const [bad, setBad] = useState(false)
  useEffect(() => { setT(format(value)); setBad(false) }, [JSON.stringify(value)])  // eslint-disable-line react-hooks/exhaustive-deps
  const commit = () => {
    const p = parse(t)
    if (p === null) { setBad(true); return }
    setBad(false)
    if (JSON.stringify(p) !== JSON.stringify(value)) onCommit(p)
  }
  return (
    <input className={`input ${bad ? 'bad' : ''}`} value={t} placeholder={placeholder} aria-label={label} aria-invalid={bad} style={{ width }}
      onChange={(e) => setT(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
  )
}

export function Switch({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" className={`switch ${on ? 'on' : ''}`} role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)}><span /></button>
  )
}

/** One choice out of a few, saved when you pick (the `.seg` control; the pick is a radio group for assistive tech). */
export function Seg<T extends string>({ value, options, onChange, label, disabled }: { value: T; options: [T, string][]; onChange: (v: T) => void; label: string; disabled?: boolean }) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map(([k, text]) => (
        <button key={k} type="button" role="radio" aria-checked={value === k} className={value === k ? 'on' : ''} disabled={disabled} onClick={() => value !== k && onChange(k)}>{text}</button>
      ))}
    </div>
  )
}

/** Toggle chips for picking several options. */
export function Chips({ options, value, onChange, label }: { options: { id: string; label: string }[]; value: string[]; onChange: (v: string[]) => void; label: string }) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((o) => {
        const on = value.includes(o.id)
        return (
          <button key={o.id} type="button" className={`chip-opt ${on ? 'on' : ''}`} aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== o.id) : [...options.map((x) => x.id)].filter((x) => x === o.id || value.includes(x)))}>
            {on ? '✓ ' : ''}{o.label}
          </button>
        )
      })}
    </div>
  )
}

/** Every model from the given providers, grouped, with an opencode provider's whole catalog. */
export function useAllModels(providers: S['ProviderInfo'][]): { p: S['ProviderInfo']; models: ModelChoice[] }[] {
  const go = providers.find((p) => p.kind === 'opencode-go')
  const catalog = useProviderCatalog(go?.id ?? 'opencode-go', !!go)
  return providers.map((p) => {
    if (!p.models) return { p, models: modelChoices(p) }
    const enabled = modelChoices(p)
    const rest = (catalog.data ?? []).filter((m) => !m.enabled).map((m) => ({
      id: m.id, label: m.name, efforts: m.variants ?? [], price: m.input === 0 && m.output === 0 ? 'free' : `$${m.input ?? '?'} / $${m.output ?? '?'} per M`,
    }))
    return { p, models: [...enabled, ...rest] }
  })
}

/** A searchable model dropdown across providers, and the effort as its own dropdown.
 * `providers` limits which providers it offers; `only` limits the models (e.g. a level's). */
export function ModelPicker({ providers, provider, tier, onChange, label, fixedProvider, compact }: {
  providers: S['ProviderInfo'][]; provider: string; tier: string; onChange: (provider: string, tier: string) => void; label: string
  fixedProvider?: boolean; compact?: boolean
}) {
  const usable = providers.filter((p) => p.enabled && p.has_credential && (!fixedProvider || p.id === provider))
  const groups = useAllModels(usable)
  const [model, effort] = splitTier(tier)
  const cur = groups.find((g) => g.p.id === provider)?.models.find((m) => m.id === model)
  const efforts = cur?.efforts ?? []
  const pick = (pid: string, m: ModelChoice) => {
    const e = effort && m.efforts.includes(effort) ? effort : m.efforts.includes('medium') ? 'medium' : m.efforts[0] ?? null
    onChange(pid, joinTier(m.id, e))
  }
  return (
    <span className={`model-picker ${compact ? 'compact' : ''}`}>
      <Combo label={label} text={cur ? `${cur.label}${usable.length > 1 && !fixedProvider ? ` · ${usable.find((p) => p.id === provider)?.label ?? provider}` : ''}` : model.replace(/^[^/]+\//, '')}
        groups={groups.map(({ p, models }) => ({ id: p.id, label: p.label, items: models.map((m) => ({ id: m.id, label: m.label, meta: m.price, on: p.id === provider && m.id === model })) }))}
        onPick={(gid, id) => { const g = groups.find((x) => x.p.id === gid)!; pick(gid, g.models.find((m) => m.id === id)!) }} compact={compact} />
      {efforts.length > 0 && (
        <Select variant={compact ? 'pick' : 'input'} value={effort ?? ''} label={`${label} effort`}
          options={efforts.map((e) => ({ value: e, label: e }))}
          onChange={(v) => onChange(provider, joinTier(model, v || null))} />
      )}
    </span>
  )
}

type ComboItem = { id: string; label: string; meta?: string; on?: boolean }

/** A button that opens a searchable list (portal-rendered, so it isn't clipped). */
function Combo({ label, text, groups, onPick, compact }: { label: string; text: string; groups: { id: string; label: string; items: ComboItem[] }[]; onPick: (group: string, id: string) => void; compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const [pos, setPos] = useState<{ left: number; top: number; width: number; up: boolean; maxHeight: number } | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const base = useId()
  const close = (refocus = true) => { setOpen(false); if (refocus) btn.current?.focus() }
  useEscape(open, close)
  const show = () => {
    const r = btn.current!.getBoundingClientRect()
    const roomBelow = window.innerHeight - r.bottom - 14
    const roomAbove = r.top - 14
    const up = roomBelow < 340 && roomAbove > roomBelow
    const maxHeight = Math.max(120, Math.min(340, up ? roomAbove : roomBelow))
    const width = Math.min(340, window.innerWidth - 16)
    setPos({ left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)), top: up ? r.top - 6 : r.bottom + 6, width, up, maxHeight })
    setQ('')
    setActive(Math.max(0, groups.flatMap((g) => g.items).findIndex((i) => i.on)))
    setOpen(true)
  }
  const ql = q.trim().toLowerCase()
  const shown = groups.map((g) => ({ ...g, items: g.items.filter((i) => !ql || `${i.label} ${i.id} ${g.label}`.toLowerCase().includes(ql)) })).filter((g) => g.items.length)
  const flat = shown.flatMap((g) => g.items.map((i) => ({ g, i })))
  const act = flat.length ? Math.min(active, flat.length - 1) : -1
  useEffect(() => { if (open && act >= 0) list.current?.querySelector<HTMLElement>(`[data-i="${act}"]`)?.scrollIntoView({ block: 'nearest' }) }, [act, open])
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (flat.length) setActive(Math.min(act + 1, flat.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (flat.length) setActive(Math.max(act - 1, 0)) }
    else if (e.key === 'Home') { e.preventDefault(); setActive(0) }
    else if (e.key === 'End') { e.preventDefault(); if (flat.length) setActive(flat.length - 1) }
    else if (e.key === 'Tab') { close() }
    else if (e.key === 'Enter') { e.preventDefault(); const p = flat[act]; if (p) { onPick(p.g.id, p.i.id); close() } }
  }
  return <>
    <button ref={btn} type="button" className={compact ? 'model-pick combo-btn' : 'input combo-btn'} aria-label={label} aria-haspopup="listbox" aria-expanded={open} onClick={() => (open ? close() : show())}>
      <span className="ellipsis">{text}</span>
    </button>
    {open && pos && createPortal(
      <div className="combo-scrim" onClick={() => close(false)}>
        <div className="combo-pop" role="listbox" aria-label={label} id={`${base}-list`} onClick={(e) => e.stopPropagation()}
          style={{ left: pos.left, width: pos.width, maxHeight: pos.maxHeight, ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }) }}>
          <input autoFocus className="combo-search" placeholder="Search models" value={q} onChange={(e) => { setQ(e.target.value); setActive(0) }} aria-label={`Search ${label}`}
            role="combobox" aria-expanded={open} aria-controls={`${base}-list`} aria-autocomplete="list"
            aria-activedescendant={act >= 0 ? `${base}-opt-${act}` : undefined} onKeyDown={onKey} />
          <div className="combo-list" ref={list}>
            {shown.map((g) => (
              <div key={g.id}>
                {groups.length > 1 && <div className="combo-group">{g.label}</div>}
                {g.items.map((i) => {
                  const n = flat.findIndex((x) => x.g === g && x.i === i)
                  return (
                    <button key={i.id} id={`${base}-opt-${n}`} data-i={n} type="button" role="option" aria-selected={!!i.on} className={`combo-item ${i.on ? 'on' : ''}`}
                      style={n === act && !i.on ? { background: 'var(--rowh)' } : undefined} onMouseEnter={() => setActive(n)} onClick={() => { onPick(g.id, i.id); close() }}>
                      <span className="stack" style={{ gap: 1, minWidth: 0 }}><span className="ellipsis">{i.label}</span>{i.id !== i.label.toLowerCase() && <span className="sub mono ellipsis">{i.id}</span>}</span>
                      {i.meta && <span className="sub">{i.meta}</span>}
                    </button>
                  )
                })}
              </div>
            ))}
            {!shown.length && <div className="sub" style={{ padding: 12 }}>No model matches “{q}”.</div>}
          </div>
        </div>
      </div>,
      document.body,
    )}
  </>
}
