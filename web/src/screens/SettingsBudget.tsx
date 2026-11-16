import { type ReactNode, useEffect, useState } from 'react'
import type { S } from '../api/client'
import {
  problem, useAddApiProvider, useBoost, useBudget, useClearBoost, useDeleteProvider, useInstructions, usePatchBudget, usePatchProvider, usePatchReserve,
  useProviders, useReviewer, useRoles, useSetInstructions, useSetReviewer, useSetRoles,
} from '../api/queries'

// Settings → Budgets & levels, Providers, Roles, Reviewer, Standing instructions (M4).
// Layout from the mockup's Budgets & levels and Roles tabs; the editable fields, the
// provider, reviewer and instructions tabs are designed in its style (see design/mockup/REVIEW.md).
// Budget changes are online only: they're never queued offline.

const cap = (s: string) => s[0].toUpperCase() + s.slice(1)
const LEVEL_ORDER: S['ProviderBudget']['level'][] = ['dormant', 'frugal', 'steady', 'busy', 'max']
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export function hoursText(ws: S['AllowedWindow'][]): string {
  if (!ws.length) return 'none'
  return ws.map((w) => {
    const span = w.start === w.end ? 'all day' : `${w.start} – ${w.end}`
    const d = [...w.days].sort().join(',')
    const days = d === '0,1,2,3,4,5,6' ? '' : d === '0,1,2,3,4' ? ' weekdays' : d === '5,6' ? ' weekends' : ' ' + w.days.map((i) => DAYS[i]).join(',')
    return span + days
  }).join(', ')
}

function parseHours(text: string): S['AllowedWindow'][] | null {
  const out: S['AllowedWindow'][] = []
  for (const part of text.split(',').map((p) => p.trim()).filter(Boolean)) {
    if (part === 'all day') { out.push({ days: [0, 1, 2, 3, 4, 5, 6], start: '00:00', end: '00:00' }); continue }
    const m = part.match(/^(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})$/)
    if (!m) return null
    out.push({ days: [0, 1, 2, 3, 4, 5, 6], start: m[1].padStart(5, '0'), end: m[2].padStart(5, '0') })
  }
  return out
}

/** A value in the mockup's field grid that turns into an editor when clicked. */
function Field({ k, hint, value, custom, onReset, children }: { k: string; hint: string; value: string; custom?: boolean; onReset?: () => void; children?: (close: () => void) => ReactNode }) {
  const [editing, setEditing] = useState(false)
  return (
    <div className="budget-field">
      <div className="stack" style={{ gap: 2, minWidth: 0 }}>
        <span style={{ font: '500 13.5px var(--sans)' }}>{k}{custom && <span className="custom-tag">custom</span>}</span>
        <span className="sub" style={{ fontSize: 12 }}>{hint}</span>
        {editing && children && <div style={{ marginTop: 6 }}>{children(() => setEditing(false))}</div>}
      </div>
      <div className="stack" style={{ gap: 4, alignItems: 'flex-end' }}>
        <button className="value-chip" onClick={() => children && setEditing(!editing)} disabled={!children}>{value}</button>
        {custom && onReset && <button className="link-btn" style={{ fontSize: 11.5 }} onClick={onReset}>Use the level's</button>}
      </div>
    </div>
  )
}

function NumberEdit({ value, onSave, min = 0, max, step = 1, suffix, close }: { value: number; onSave: (v: number) => void; min?: number; max?: number; step?: number; suffix?: string; close: () => void }) {
  const [v, setV] = useState(String(value))
  return (
    <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); onSave(Number(v)); close() }}>
      <input className="input" type="number" min={min} max={max} step={step} value={v} onChange={(e) => setV(e.target.value)} autoFocus />
      {suffix && <span className="sub">{suffix}</span>}
      <button className="btn small primary">Save</button>
    </form>
  )
}

export function Budgets() {
  const budget = useBudget()
  const patch = usePatchBudget()
  const reserve = usePatchReserve()
  const boost = useBoost()
  const endBoost = useClearBoost()
  const [err, setErr] = useState<string | null>(null)
  const o = budget.data
  if (budget.isError) return <div className="error">Couldn't load budgets.</div>
  if (!o) return null
  const b = o.providers[0]  // the worker's provider
  const lim = b.limits
  const done = { onError: (x: unknown) => setErr(problem(x)), onSuccess: () => setErr(null) }
  const override = (ov: S['BudgetLimitsPatch']) => patch.mutate({ provider: b.provider, overrides: ov }, done)
  const isCustom = (k: string) => b.custom.includes(k)
  const next = LEVEL_ORDER[Math.min(LEVEL_ORDER.length - 1, LEVEL_ORDER.indexOf(b.level) + 1)]
  const levelText = o.levels.find((l) => l.name === b.effective_level)?.text ?? ''
  return (
    <div className="stack" style={{ gap: 28 }}>
      {err && <div className="error" role="alert">{err}</div>}
      <section className="set-section">
        <div className="set-title">Level</div>
        <div className="level-grid">
          {o.levels.map((l) => (
            <button key={l.name} className={`level-opt ${b.level === l.name ? 'on' : ''}`} aria-pressed={b.level === l.name} onClick={() => patch.mutate({ provider: b.provider, level: l.name }, done)}>
              <span style={{ font: '500 14px var(--sans)' }}>{cap(l.name)}</span>
              <span className="sub" style={{ fontSize: 12 }}>{l.limits.allowance ? `${l.limits.allowance} pts/wk` : 'Reply now only'}</span>
            </button>
          ))}
        </div>
        <div className="level-text">{levelText}{b.custom.length ? ' You changed some of its limits below.' : ''}</div>
        <div className="sub" style={{ fontSize: 12.5 }}>1 pt is 0.1% of your Claude subscription's week (1,000 pts a week), or $0.10 on an API key.</div>
      </section>

      <section className="set-section">
        <div className="set-title">Provider allowances</div>
        <div className="card-flat">
          {o.providers.map((p) => {
            const week = p.kind === 'claude-subscription' ? 1000 : Math.max(p.allowance, 1)
            const used = p.used_by_assistant + p.used_by_owner + p.used_by_chat
            return (
              <div key={p.provider} className="allowance-row">
                <span style={{ font: '500 13.5px var(--sans)' }}>{p.label}</span>
                <div className="alloc-bar">
                  <div style={{ width: `${Math.min(100, (used / week) * 100)}%`, background: '#c4673f' }} />
                  {p.kind === 'claude-subscription' && <div style={{ width: `${Math.max(0, 100 - o.reserve_weekly_pct)}%`, background: 'var(--accentLine2)', marginLeft: 'auto' }} />}
                </div>
                <span className="sub" style={{ fontSize: 12.5, justifySelf: 'end', textAlign: 'right' }}>
                  {p.native} · {Math.round(p.left)} of {Math.round(p.allowance)} pts left
                </span>
              </div>
            )
          })}
        </div>
        <div className="sub" style={{ fontSize: 12.5 }}>
          This week: you {Math.round(b.used_by_owner)} pts{b.used_by_chat ? ` (+${Math.round(b.used_by_chat)} in 1:1 chats)` : ''}, the assistant {Math.round(b.used_by_assistant)} pts. {b.pace}.
        </div>
      </section>

      <div className="budget-grid">
        <Field k="Weekly allowance" hint="what the assistant may plan with" value={`${lim.allowance} pts / wk`} custom={isCustom('allowance')} onReset={() => override({ allowance: null })}>
          {(close) => <NumberEdit value={lim.allowance} suffix="pts" onSave={(v) => override({ allowance: v })} close={close} />}
        </Field>
        <Field k="Allowed hours" hint="when wakeups may run (the rest is yours)" value={hoursText(lim.allowed_hours)} custom={isCustom('allowed_hours')} onReset={() => override({ allowed_hours: null })}>
          {(close) => <HoursEdit value={lim.allowed_hours} onSave={(v) => override({ allowed_hours: v })} close={close} />}
        </Field>
        <Field k="Wakeup limits" hint="per day · gap · per wakeup" value={`${lim.max_per_day} max · ${lim.min_gap_minutes} min · ${lim.max_wakeup} pts`}
          custom={isCustom('max_per_day') || isCustom('min_gap_minutes') || isCustom('max_wakeup')} onReset={() => override({ max_per_day: null, min_gap_minutes: null, max_wakeup: null })}>
          {(close) => <LimitsEdit lim={lim} onSave={(v) => override(v)} close={close} />}
        </Field>
        <Field k="Reply budget" hint="for “Reply now”" value={`${b.reply_allowance} pts / wk`}>
          {(close) => <NumberEdit value={b.reply_allowance} suffix="pts" onSave={(v) => patch.mutate({ provider: b.provider, reply_pts: v }, done)} close={close} />}
        </Field>
        <Field k="Fallback" hint="if nothing is planned for 2 nights" value={lim.fallback ? `on · ${b.fallback_pts} pts` : 'off'} custom={isCustom('fallback')} onReset={() => override({ fallback: null })}>
          {(close) => (
            <div className="inline-edit">
              <button className="btn small" onClick={() => { override({ fallback: !lim.fallback }); close() }}>{lim.fallback ? 'Turn off' : 'Turn on'}</button>
              <NumberEdit value={b.fallback_pts} suffix="pts" onSave={(v) => patch.mutate({ provider: b.provider, fallback_pts: v }, done)} close={close} />
            </div>
          )}
        </Field>
        <Field k="Sweep" hint={`last ${o.sweep_window_hours} h before the reset, up to ${o.sweep_ceiling_pct}%`} value={lim.sweep ? 'on' : 'off'} custom={isCustom('sweep')} onReset={() => override({ sweep: null })}>
          {(close) => (
            <div className="inline-edit">
              <button className="btn small" onClick={() => { override({ sweep: !lim.sweep }); close() }}>{lim.sweep ? 'Turn off' : 'Turn on'}</button>
              <NumberEdit value={o.sweep_ceiling_pct} max={100} suffix="% ceiling" onSave={(v) => reserve.mutate({ sweep_ceiling_pct: v }, done)} close={close} />
            </div>
          )}
        </Field>
        <Field k="Reserve for you" hint="no wakeup starts above weekly · 5-hour" value={`${o.reserve_weekly_pct}% · ${o.reserve_five_hour_pct}%`}>
          {(close) => (
            <div className="stack" style={{ gap: 6 }}>
              <NumberEdit value={o.reserve_weekly_pct} max={100} suffix="% weekly" onSave={(v) => reserve.mutate({ reserve_weekly_pct: v }, done)} close={close} />
              <NumberEdit value={o.reserve_five_hour_pct} max={100} suffix="% 5-hour" onSave={(v) => reserve.mutate({ reserve_five_hour_pct: v }, done)} close={close} />
            </div>
          )}
        </Field>
        <Field k="Allowed tiers" hint="models the worker may use" value={lim.tiers.length ? lim.tiers.join(', ') : 'none'} custom={isCustom('tiers')} onReset={() => override({ tiers: null })}>
          {(close) => <TiersEdit all={o.tiers} value={lim.tiers} onSave={(v) => override({ tiers: v })} close={close} />}
        </Field>
      </div>

      <div className="boost-box">
        {b.boost ? (
          <>
            <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Boost: {cap(b.boost.level)}</span>
              <span className="sub" style={{ fontSize: 12.5 }}>Until {new Date(b.boost.until).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })}; then back to {cap(b.level)}.</span></div>
            <button className="btn" onClick={() => endBoost.mutate(b.provider, done)}>End boost</button>
          </>
        ) : (
          <>
            <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Boost</span>
              <span className="sub" style={{ fontSize: 12.5 }}>Raise the level to {cap(next)} for a while ({o.levels.find((l) => l.name === next)?.limits.allowance} pts/wk). Your reserve still protects your own use.</span></div>
            <div className="btns">
              <button className="btn" disabled={next === b.level} onClick={() => boost.mutate({ provider: b.provider, level: next, until_reset: true }, done)}>Until the reset</button>
              <button className="btn primary" disabled={next === b.level} onClick={() => boost.mutate({ provider: b.provider, level: next, hours: 24 }, done)}>Boost for 24 h</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function HoursEdit({ value, onSave, close }: { value: S['AllowedWindow'][]; onSave: (v: S['AllowedWindow'][]) => void; close: () => void }) {
  const [t, setT] = useState(hoursText(value))
  const parsed = parseHours(t)
  return (
    <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); if (parsed) { onSave(parsed); close() } }}>
      <input className="input" value={t} onChange={(e) => setT(e.target.value)} placeholder="22:00 – 08:00, 12:00 – 14:00" autoFocus aria-label="Allowed hours" />
      <button className="btn small primary" disabled={!parsed}>Save</button>
    </form>
  )
}

function LimitsEdit({ lim, onSave, close }: { lim: S['BudgetLimits']; onSave: (v: S['BudgetLimitsPatch']) => void; close: () => void }) {
  const [v, setV] = useState({ max_per_day: lim.max_per_day, min_gap_minutes: lim.min_gap_minutes, max_wakeup: lim.max_wakeup })
  return (
    <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); onSave(v); close() }}>
      <input className="input" type="number" min={0} value={v.max_per_day} onChange={(e) => setV({ ...v, max_per_day: Number(e.target.value) })} aria-label="Per day" title="Wakeups per day" />
      <input className="input" type="number" min={0} value={v.min_gap_minutes} onChange={(e) => setV({ ...v, min_gap_minutes: Number(e.target.value) })} aria-label="Gap (min)" title="Minutes between wakeups" />
      <input className="input" type="number" min={1} value={v.max_wakeup} onChange={(e) => setV({ ...v, max_wakeup: Number(e.target.value) })} aria-label="Pts per wakeup" title="Most points per wakeup" />
      <button className="btn small primary">Save</button>
    </form>
  )
}

function TiersEdit({ all, value, onSave, close }: { all: string[]; value: string[]; onSave: (v: string[]) => void; close: () => void }) {
  const [sel, setSel] = useState(value)
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="tier-checks">
        {all.map((t) => <label key={t}><input type="checkbox" checked={sel.includes(t)} onChange={(e) => setSel(e.target.checked ? [...sel, t] : sel.filter((x) => x !== t))} /> {t}</label>)}
      </div>
      <div><button className="btn small primary" onClick={() => { onSave(all.filter((t) => sel.includes(t))); close() }}>Save</button></div>
    </div>
  )
}

// ── Providers ────────────────────────────────────────────────────────────
export function Providers() {
  const providers = useProviders()
  const add = useAddApiProvider()
  const patch = usePatchProvider()
  const del = useDeleteProvider()
  const [key, setKey] = useState('')
  const [goKey, setGoKey] = useState('')
  const hasGo = providers.data?.some((p) => p.kind === 'opencode-go')
  return (
    <div className="stack" style={{ gap: 24 }}>
      <section className="set-section">
        <div className="set-title">Providers</div>
        <div className="stack" style={{ gap: 8 }}>
          {providers.data?.map((p) => (
            <div key={p.id} className="provider-card">
              <div className="stack" style={{ gap: 2 }}>
                <span style={{ font: '500 14px var(--sans)' }}>{p.label} {!p.enabled && <span className="pill" style={{ marginLeft: 6 }}>off</span>}</span>
                <span className="sub" style={{ fontSize: 12.5 }}>{p.status}</span>
              </div>
              <div className="btns">
                <button className="btn small" onClick={() => patch.mutate({ id: p.id, enabled: !p.enabled })}>{p.enabled ? 'Turn off' : 'Turn on'}</button>
                {p.kind !== 'claude-subscription' && <button className="btn small danger" onClick={() => del.mutate(p.id)}>Remove</button>}
              </div>
              {p.models && <TierModels provider={p} />}
            </div>
          ))}
        </div>
        {patch.error && <div className="error" role="alert">{problem(patch.error)}</div>}
        <div className="sub" style={{ fontSize: 12.5 }}>
          The Claude subscription uses the login on the assistant's computer; usage comes from its weekly and 5-hour meter. Logging in through this page arrives with the setup wizard (M5).
        </div>
      </section>
      <section className="set-section">
        <div className="set-title">Anthropic API key</div>
        <div className="sub" style={{ fontSize: 13 }}>Write-only: stored encrypted and given only to sessions that run on this provider. Usage is the runtime's own cost, 1 pt per $0.10.</div>
        <form className="set-form" onSubmit={(e) => { e.preventDefault(); add.mutate({ key, label: null }, { onSuccess: () => setKey('') }) }}>
          <input className="input" type="password" autoComplete="off" placeholder="sk-ant-…" value={key} onChange={(e) => setKey(e.target.value)} aria-label="API key" />
          {add.error && <div className="error" role="alert">{problem(add.error)}</div>}
          {add.isSuccess && <div className="done">Saved.</div>}
          <div><button className="btn primary" disabled={key.length < 20 || add.isPending}>Save key</button></div>
        </form>
      </section>
      <section className="set-section">
        <div className="set-title">opencode Go</div>
        <div className="sub" style={{ fontSize: 13 }}>
          A subscription to open models, run with opencode instead of Claude Code. Paste the key from opencode.ai (your workspace → API keys).
          Write-only, like the API key. Each step's cost is reported by opencode at catalog prices, 1 pt per $0.10; Go's own limits apply too.
        </div>
        <form className="set-form" onSubmit={(e) => { e.preventDefault(); add.mutate({ key: goKey, label: null, kind: 'opencode-go', use_for_all_roles: false }, { onSuccess: () => setGoKey('') }) }}>
          <input className="input" type="password" autoComplete="off" placeholder={hasGo ? 'Replace the saved key' : 'opencode API key'} value={goKey} onChange={(e) => setGoKey(e.target.value)} aria-label="opencode Go key" />
          <div><button className="btn primary" disabled={goKey.length < 20 || add.isPending}>Save key</button></div>
        </form>
        {hasGo && <div className="sub" style={{ fontSize: 12.5 }}>Choose which roles run on it under Roles.</div>}
      </section>
    </div>
  )
}

const TIER_NAMES: [string, string][] = [['haiku', 'Small (haiku)'], ['sonnet', 'Medium (sonnet)'], ['opus', 'Large (opus)']]

function TierModels({ provider }: { provider: S['ProviderInfo'] }) {
  const patch = usePatchProvider()
  const saved = Object.fromEntries(Object.entries(provider.models ?? {}).map(([k, v]) => [k, v.model]))
  const [m, setM] = useState<Record<string, string>>(saved)
  useEffect(() => setM(saved), [JSON.stringify(saved)])  // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(m) !== JSON.stringify(saved)
  return (
    <div className="stack" style={{ gap: 6, gridColumn: '1 / -1', marginTop: 8 }}>
      <span className="sub" style={{ fontSize: 12.5 }}>The model behind each tier name (as <code>opencode models opencode-go</code> lists them):</span>
      {TIER_NAMES.map(([k, label]) => (
        <label key={k} className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span style={{ width: 120, fontSize: 13 }}>{label}</span>
          <input className="input mono" style={{ flex: 1 }} value={m[k] ?? ''} onChange={(e) => setM({ ...m, [k]: e.target.value })} aria-label={`${label} model`} />
        </label>
      ))}
      {patch.error && <div className="error" role="alert">{problem(patch.error)}</div>}
      {dirty && <div><button className="btn small primary" disabled={patch.isPending} onClick={() => patch.mutate({ id: provider.id, models: m })}>Save models</button></div>}
    </div>
  )
}

// ── Roles ────────────────────────────────────────────────────────────────
export function Roles() {
  const roles = useRoles()
  const providers = useProviders()
  const budget = useBudget()
  const set = useSetRoles()
  const [edit, setEdit] = useState<S['RoleConfig'][] | null>(null)
  useEffect(() => { if (roles.data) setEdit(roles.data.roles) }, [roles.data])
  if (!edit) return null
  const tiers = budget.data?.tiers ?? []
  const change = (i: number, upd: Partial<S['RoleConfig']>) => setEdit(edit.map((r, j) => (i === j ? { ...r, ...upd } : r)))
  const dirty = JSON.stringify(edit) !== JSON.stringify(roles.data?.roles)
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="card-flat">
        <div className="roles-row head"><span>Role</span><span>Provider</span><span>Model</span><span>Effort</span><span>Allowed range</span></div>
        {edit.map((r, i) => {
          const [model, effort] = r.tier.split('/')
          return (
            <div key={r.role} className="roles-row">
              <span style={{ fontWeight: 500 }} title={r.note ?? ''}>{cap(r.role)}</span>
              <select className="input" value={r.provider} onChange={(e) => change(i, { provider: e.target.value })} aria-label={`${r.role} provider`}>
                {providers.data?.filter((p) => p.enabled).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
              <select className="input mono" value={model} onChange={(e) => change(i, { tier: `${e.target.value}/${effort ?? 'medium'}` })} aria-label={`${r.role} model`}>
                {['haiku', 'sonnet', 'opus'].map((m) => {
                  const mapped = providers.data?.find((p) => p.id === r.provider)?.models?.[m]?.model
                  return <option key={m} value={m}>{mapped ? `${m} · ${mapped.replace(/^opencode-go\//, '')}` : m}</option>
                })}
              </select>
              <select className="input" value={effort ?? 'medium'} onChange={(e) => change(i, { tier: `${model}/${e.target.value}` })} aria-label={`${r.role} effort`}>
                {['low', 'medium', 'high'].map((m) => <option key={m}>{m}</option>)}
              </select>
              <span className="sub" style={{ fontSize: 12.5 }}>{r.role === 'worker' ? `${r.allowed.join(', ') || 'none'} (from your level)` : r.note}</span>
            </div>
          )
        })}
      </div>
      {set.error && <div className="error" role="alert">{problem(set.error)}</div>}
      {tiers.length > 0 && dirty && <div><button className="btn primary" disabled={set.isPending} onClick={() => set.mutate({ roles: edit })}>Save roles</button></div>}
    </div>
  )
}

// ── Reviewer ─────────────────────────────────────────────────────────────
type RC = Required<S['ReviewerConfig']>
const MODES: [RC['mode'], string, string][] = [
  ['off', 'Off', 'Wakeups run without a reviewer.'],
  ['advisor', 'Advisor', 'Answers when the assistant asks.'],
  ['monitor', 'Monitor', 'Also reads its notes and may interject.'],
  ['gate', 'Gate', 'Also approves finished tasks and the end of a wakeup.'],
]

export function Reviewer() {
  const rv = useReviewer()
  const set = useSetReviewer()
  const budget = useBudget()
  const [c, setC] = useState<RC | null>(null)
  useEffect(() => { if (rv.data) setC(rv.data as RC) }, [rv.data])
  if (!c) return null
  const save = (upd: Partial<RC>) => { const n = { ...c, ...upd }; setC(n); set.mutate(n) }
  return (
    <div className="stack" style={{ gap: 24 }}>
      <section className="set-section">
        <div className="set-title">Mode</div>
        <div className="level-grid" style={{ gridTemplateColumns: 'repeat(4, minmax(0, 1fr))' }}>
          {MODES.map(([m, label, text]) => (
            <button key={m} className={`level-opt ${c.mode === m ? 'on' : ''}`} aria-pressed={c.mode === m} onClick={() => save({ mode: m })}>
              <span style={{ font: '500 14px var(--sans)' }}>{label}</span><span className="sub" style={{ fontSize: 12 }}>{text}</span>
            </button>
          ))}
        </div>
        <div className="sub" style={{ fontSize: 12.5 }}>The reviewer is a stronger model that watches wakeups and routines (not quick replies). It reads the assistant's memory but can't change anything.</div>
      </section>
      <section className="set-section">
        <div className="set-title">Settings</div>
        <div className="budget-grid">
          <Field k="Model" hint="its tier" value={c.tier}>{(close) => (
            <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); close() }}>
              <select className="input" value={c.tier} onChange={(e) => save({ tier: e.target.value })}>{(budget.data?.tiers ?? [c.tier]).map((t) => <option key={t}>{t}</option>)}</select>
            </form>
          )}</Field>
          <Field k="Watch interval" hint="how often it reads new notes" value={`${c.interval_minutes} min`}>
            {(close) => <NumberEdit value={c.interval_minutes} min={1} suffix="min" onSave={(v) => save({ interval_minutes: v })} close={close} />}
          </Field>
          <Field k="Budget share" hint="most of a wakeup's budget it may use" value={`${Math.round(c.budget_share * 100)}%`}>
            {(close) => <NumberEdit value={Math.round(c.budget_share * 100)} min={1} max={100} suffix="%" onSave={(v) => save({ budget_share: v / 100 })} close={close} />}
          </Field>
          <Field k="Continuity" hint="one conversation per…" value={c.continuity === 'day' ? 'day' : 'wakeup'}>
            {(close) => <div className="inline-edit"><button className="btn small" onClick={() => { save({ continuity: c.continuity === 'day' ? 'wakeup' : 'day' }); close() }}>Switch to per {c.continuity === 'day' ? 'wakeup' : 'day'}</button></div>}
          </Field>
          <Field k="Change the worker's tier" hint={`up to ${c.max_tier_changes} times a wakeup`} value={c.change_tier ? `on · ${c.tier_choices.join(', ')}` : 'off'}>
            {(close) => (
              <div className="stack" style={{ gap: 6 }}>
                <button className="btn small" onClick={() => save({ change_tier: !c.change_tier })}>{c.change_tier ? 'Turn off' : 'Turn on'}</button>
                <TiersEdit all={budget.data?.tiers ?? c.tier_choices} value={c.tier_choices} onSave={(v) => save({ tier_choices: v })} close={close} />
              </div>
            )}
          </Field>
        </div>
        {set.error && <div className="error" role="alert">{problem(set.error)}</div>}
      </section>
    </div>
  )
}

// ── Standing instructions ────────────────────────────────────────────────
export function Instructions() {
  const ins = useInstructions()
  const set = useSetInstructions()
  const [t, setT] = useState<string | null>(null)
  useEffect(() => { if (ins.data && t === null) setT(ins.data.text) }, [ins.data, t])
  if (t === null) return null
  return (
    <section className="set-section">
      <div className="set-title">Standing instructions</div>
      <div className="sub" style={{ fontSize: 13 }}>What the assistant should always keep in mind. They open every brief; it hears when you change them.</div>
      <textarea className="textarea" rows={10} value={t} onChange={(e) => setT(e.target.value)} aria-label="Standing instructions" maxLength={8000} />
      {set.error && <div className="error" role="alert">{problem(set.error)}</div>}
      <div className="row-between">
        <span className="sub" style={{ fontSize: 12 }}>{ins.data?.updated_at ? `Saved ${new Date(ins.data.updated_at).toLocaleString()}` : 'Not set yet'}</span>
        <button className="btn primary" disabled={set.isPending || t === ins.data?.text} onClick={() => set.mutate(t)}>Save</button>
      </div>
    </section>
  )
}
