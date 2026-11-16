import { type ReactNode, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { S } from '../api/client'
import { useProviderCatalog } from '../api/queries'
import { joinTier, modelChoices, splitTier, type ModelChoice } from './models'

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
        <select className={compact ? 'model-pick' : 'input'} value={effort ?? ''} aria-label={`${label} effort`} onChange={(e) => onChange(provider, joinTier(model, e.target.value || null))} style={compact ? undefined : { width: 'auto' }}>
          {efforts.map((e) => <option key={e} value={e}>{e}</option>)}
        </select>
      )}
    </span>
  )
}

type ComboItem = { id: string; label: string; meta?: string; on?: boolean }

/** A button that opens a searchable list (portal-rendered, so it isn't clipped). */
function Combo({ label, text, groups, onPick, compact }: { label: string; text: string; groups: { id: string; label: string; items: ComboItem[] }[]; onPick: (group: string, id: string) => void; compact?: boolean }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [pos, setPos] = useState<{ left: number; top: number; width: number; up: boolean } | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const show = () => {
    const r = btn.current!.getBoundingClientRect()
    const up = r.bottom + 340 > window.innerHeight && r.top > 340
    setPos({ left: Math.min(r.left, window.innerWidth - 340), top: up ? r.top - 6 : r.bottom + 6, width: Math.max(r.width, 320), up })
    setQ('')
    setOpen(true)
  }
  const ql = q.trim().toLowerCase()
  const shown = groups.map((g) => ({ ...g, items: g.items.filter((i) => !ql || `${i.label} ${i.id} ${g.label}`.toLowerCase().includes(ql)) })).filter((g) => g.items.length)
  const first = shown[0]?.items[0]
  return <>
    <button ref={btn} type="button" className={compact ? 'model-pick combo-btn' : 'input combo-btn'} aria-label={label} aria-haspopup="listbox" aria-expanded={open} onClick={() => (open ? setOpen(false) : show())}>
      <span className="ellipsis">{text}</span>
    </button>
    {open && pos && createPortal(
      <div className="combo-scrim" onClick={() => setOpen(false)}>
        <div className="combo-pop" role="listbox" aria-label={label} onClick={(e) => e.stopPropagation()}
          style={{ left: pos.left, width: pos.width, ...(pos.up ? { bottom: window.innerHeight - pos.top } : { top: pos.top }) }}>
          <input autoFocus className="combo-search" placeholder="Search models" value={q} onChange={(e) => setQ(e.target.value)} aria-label={`Search ${label}`}
            onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); if (e.key === 'Enter' && first) { onPick(shown[0].id, first.id); setOpen(false) } }} />
          <div className="combo-list">
            {shown.map((g) => (
              <div key={g.id}>
                {groups.length > 1 && <div className="combo-group">{g.label}</div>}
                {g.items.map((i) => (
                  <button key={i.id} type="button" role="option" aria-selected={!!i.on} className={`combo-item ${i.on ? 'on' : ''}`} onClick={() => { onPick(g.id, i.id); setOpen(false) }}>
                    <span className="stack" style={{ gap: 1, minWidth: 0 }}><span className="ellipsis">{i.label}</span>{i.id !== i.label.toLowerCase() && <span className="sub mono ellipsis">{i.id}</span>}</span>
                    {i.meta && <span className="sub">{i.meta}</span>}
                  </button>
                ))}
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
