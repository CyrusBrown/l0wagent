import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { S } from '../api/client'
import { useTimeFormat } from '../app/timeFormat'
import { stamp } from '../components/format'
import { modelChoices, tierLabel } from '../components/models'
import { ClaudeLogin } from './Setup'
import { Chips, ModelPicker, NumberField, Row, Saved, Switch, TextField } from '../components/settings'
import { usePresence } from '../components/motion'
import { Select } from '../components/Select'
import { useEscape } from '../components/useEscape'
import {
  problem, useAddApiProvider, useAddBonus, useBoost, useBudget, useClearBonus, useClearBoost, useDeleteProvider, useInstructions, usePatchBudget, usePatchProvider, usePatchReserve,
  useProviderCatalog, useProviders, useReviewer, useSystem, useRoles, useSetInstructions, useSetReviewer, useSetRoles,
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

// The level's limits are edited as a draft and saved together. A limit set to null goes back to the level's value.
type Draft = {
  allowance?: number | null; allowed_hours?: S['AllowedWindow'][] | null; max_per_day?: number | null; min_gap_minutes?: number | null
  max_wakeup?: number | null; tiers?: string[] | null; sweep?: boolean | null; fallback?: boolean | null
  fallback_pts?: number; sweep_ceiling_pct?: number; sweep_window_hours?: number
}
const LIMIT_KEYS = ['allowance', 'allowed_hours', 'max_per_day', 'min_gap_minutes', 'max_wakeup', 'tiers', 'sweep', 'fallback'] as const

export function Budgets() {
  useTimeFormat()
  const budget = useBudget()
  const patch = usePatchBudget()
  const reserve = usePatchReserve()
  const boost = useBoost()
  const endBoost = useClearBoost()
  const addBonus = useAddBonus()
  const clearBonus = useClearBonus()
  const [bonusPts, setBonusPts] = useState(100)
  const [err, setErr] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState(0)
  const [draft, setDraft] = useState<Draft>({})
  const providers = useProviders()
  const o = budget.data
  if (budget.isError) return <div className="error">Couldn't load budgets.</div>
  if (!o) return null
  const b = o.providers[0]  // the worker's provider
  const lim = b.limits
  const done = { onError: (x: unknown) => setErr(problem(x)), onSuccess: () => { setErr(null); setSavedAt(Date.now()) } }
  const isCustom = (k: string) => b.custom.includes(k)
  const next = LEVEL_ORDER[Math.min(LEVEL_ORDER.length - 1, LEVEL_ORDER.indexOf(b.level) + 1)]
  const preset = o.levels.find((l) => l.name === b.level)!.limits
  const levelText = o.levels.find((l) => l.name === b.effective_level)?.text ?? ''
  // Presets are per week; a monthly budget scales them to the month.
  const scale = b.period === 'month' ? (Date.parse(b.resets_at) - Date.parse(b.week_start)) / (7 * 86400_000) : 1
  const per = b.period === 'month' ? 'month' : 'week'
  const ptsPer = (weekly: number) => `${Math.round(weekly * scale)} pts / ${per}`
  const L = cap(b.level)
  const presetFor = (k: (typeof LIMIT_KEYS)[number]) => (k === 'allowance' ? Math.round(preset.allowance * scale) : preset[k])
  const same = (x: unknown, y: unknown) => JSON.stringify(x) === JSON.stringify(y)
  // What the form shows: the draft where there is one, else what's saved.
  const val = <K extends (typeof LIMIT_KEYS)[number]>(k: K): S['BudgetLimits'][K] =>
    (k in draft ? (draft[k] === null ? presetFor(k) : draft[k]) : lim[k]) as S['BudgetLimits'][K]
  const setLim = <K extends (typeof LIMIT_KEYS)[number]>(k: K, v: Draft[K]) => setDraft((d) => {
    const n = { ...d }
    if (v === null ? !isCustom(k) : same(v, lim[k])) delete n[k]
    else (n as Record<string, unknown>)[k] = v
    return n
  })
  const setRes = (k: 'fallback_pts' | 'sweep_ceiling_pct' | 'sweep_window_hours', v: number, saved: number) => setDraft((d) => {
    const n = { ...d }
    if (v === saved) delete n[k]; else n[k] = v
    return n
  })
  const fallbackPts = draft.fallback_pts ?? b.fallback_pts
  const sweepCeiling = draft.sweep_ceiling_pct ?? o.sweep_ceiling_pct
  const sweepWindow = draft.sweep_window_hours ?? o.sweep_window_hours
  const dirty = Object.keys(draft).length > 0
  const wasShown = (k: (typeof LIMIT_KEYS)[number]) => (k in draft ? draft[k] !== null : isCustom(k))
  const discard = () => { setDraft({}); setErr(null) }
  const save = () => {
    const overrides: Record<string, unknown> = {}
    for (const k of LIMIT_KEYS) if (k in draft) overrides[k] = draft[k]
    const res: S['ReserveSettings'] = {}
    if (draft.sweep_ceiling_pct !== undefined) res.sweep_ceiling_pct = draft.sweep_ceiling_pct
    if (draft.sweep_window_hours !== undefined) res.sweep_window_hours = draft.sweep_window_hours
    const finish = { onError: done.onError, onSuccess: () => { done.onSuccess(); setDraft({}) } }
    const sendRes = () => (Object.keys(res).length ? reserve.mutate(res, finish) : finish.onSuccess())
    const body: S['BudgetPatch'] = {}
    if (Object.keys(overrides).length) body.overrides = overrides as S['BudgetLimitsPatch']
    if (draft.fallback_pts !== undefined) body.fallback_pts = draft.fallback_pts
    if (Object.keys(body).length) patch.mutate({ provider: b.provider, ...body }, { onError: done.onError, onSuccess: sendRes })
    else sendRes()
  }
  const changed: string[] = []
  if (isCustom('allowance')) changed.push(`allowance ${lim.allowance} (${L}: ${Math.round(preset.allowance * scale)})`)
  if (isCustom('allowed_hours')) changed.push('allowed hours')
  if (isCustom('max_per_day') || isCustom('min_gap_minutes') || isCustom('max_wakeup')) changed.push('wakeup limits')
  if (isCustom('tiers')) changed.push('models')
  if (isCustom('fallback')) changed.push('fallback')
  if (isCustom('sweep')) changed.push('sweep')
  const resetAll = () => patch.mutate({ provider: b.provider, level: b.level }, { onError: done.onError, onSuccess: () => { done.onSuccess(); setDraft({}) } })  // choosing the level clears overrides
  const was = (k: (typeof LIMIT_KEYS)[number], text: string) => (wasShown(k) ? `${L}: ${text}` : undefined)
  return (
    <div className="stack" style={{ gap: 28 }}>
      {err && <div className="error" role="alert">{err}</div>}
      <section className="set-section">
        <div className="set-title">Level{b.kind !== 'claude-subscription' ? ` · ${b.label}` : ''}</div>
        <div className="level-grid">
          {o.levels.map((l) => (
            <button key={l.name} className={`level-opt ${b.level === l.name ? 'on' : ''}`} aria-pressed={b.level === l.name} onClick={() => patch.mutate({ provider: b.provider, level: l.name }, done)}>
              <span style={{ font: '500 14px var(--sans)' }}>{cap(l.name)}</span>
              <span className="sub" style={{ fontSize: 12 }}>{l.limits.allowance ? ptsPer(l.limits.allowance) : 'Reply now only'}</span>
            </button>
          ))}
        </div>
        <div className="level-text">{levelText}</div>
        <div className="sub" style={{ fontSize: 12.5 }}>
          1 pt is 0.1% of your Claude subscription's week (1,000 pts a week), or $0.10 on an API key or opencode Go.
        </div>
      </section>

      <section className="set-section">
        <div className="set-title">This {per}</div>
        <div className="card-flat">
          {o.providers.map((p) => {
            const whole = p.kind === 'claude-subscription' ? 1000 : Math.max(p.allowance, 1)
            const used = p.used_by_assistant + p.used_by_owner + p.used_by_chat
            return (
              <div key={p.provider} className="allowance-row">
                <span style={{ font: '500 13.5px var(--sans)' }}>{p.label}</span>
                <div className="alloc-bar">
                  <div style={{ width: `${Math.min(100, (used / whole) * 100)}%`, background: 'var(--accent)' }} />
                  {p.kind === 'claude-subscription' && <div style={{ width: `${Math.max(0, 100 - o.reserve_weekly_pct)}%`, background: 'var(--accentLine2)', marginLeft: 'auto' }} />}
                </div>
                <span className="sub" style={{ fontSize: 12.5, justifySelf: 'end', textAlign: 'right' }}>
                  {p.native} · {Math.round(p.left)} of {Math.round(p.allowance)}{p.bonus_pts ? ` + ${Math.round(p.bonus_pts)} bonus` : ''} pts left
                </span>
              </div>
            )
          })}
        </div>
        <div className="sub" style={{ fontSize: 12.5 }}>
          You {Math.round(b.used_by_owner)} pts{b.used_by_chat ? ` (+${Math.round(b.used_by_chat)} in 1:1 chats)` : ''}, the assistant {Math.round(b.used_by_assistant)} pts. {b.pace}.
          {b.cap_reason && <> <span style={{ color: 'var(--warnInk)' }}>{b.cap_reason}</span></>}
        </div>
      </section>

      <section className="set-section">
        <div className="set-head"><div className="set-title">{L}'s limits</div><Saved token={savedAt} /></div>
        <div className="sub" style={{ fontSize: 12.5, marginTop: -4 }}>Change any of them, then save. Picking a level again resets them.</div>
        <div className="set-rows">
          <Row label="Allowance" hint="what the assistant may plan with" was={was('allowance', ptsPer(preset.allowance))} onReset={() => setLim('allowance', null)}>
            <NumberField label="Allowance" value={val('allowance')} suffix={`pts / ${per}`} onCommit={(v) => setLim('allowance', v)} />
          </Row>
          <Row label="Allowed hours" hint="when wakeups may run (the rest is yours)" was={was('allowed_hours', hoursText(preset.allowed_hours))} onReset={() => setLim('allowed_hours', null)}>
            <TextField label="Allowed hours" value={val('allowed_hours')} format={hoursText} parse={parseHours} placeholder="22:00 – 08:00" onCommit={(v) => setLim('allowed_hours', v)} width={180} />
          </Row>
          <Row label="Wakeups a day" hint="at most" was={was('max_per_day', String(preset.max_per_day))} onReset={() => setLim('max_per_day', null)}>
            <NumberField label="Wakeups a day" value={val('max_per_day')} onCommit={(v) => setLim('max_per_day', v)} />
          </Row>
          <Row label="Gap between wakeups" hint="at least" was={was('min_gap_minutes', `${preset.min_gap_minutes} min`)} onReset={() => setLim('min_gap_minutes', null)}>
            <NumberField label="Gap between wakeups" value={val('min_gap_minutes')} suffix="min" onCommit={(v) => setLim('min_gap_minutes', v)} />
          </Row>
          <Row label="Per wakeup" hint="the most one wakeup may spend" was={was('max_wakeup', `${preset.max_wakeup} pts`)} onReset={() => setLim('max_wakeup', null)}>
            <NumberField label="Per wakeup" value={val('max_wakeup')} min={1} suffix="pts" onCommit={(v) => setLim('max_wakeup', v)} />
          </Row>
          <Row label="Fallback" hint="a wakeup if nothing is planned for the next nights (how many: Advanced)" was={was('fallback', preset.fallback ? 'on' : 'off')} onReset={() => setLim('fallback', null)}>
            {val('fallback') && <NumberField label="Fallback budget" value={fallbackPts} suffix="pts" onCommit={(v) => setRes('fallback_pts', v, b.fallback_pts)} width={64} />}
            <Switch label="Fallback" on={val('fallback')} onChange={(v) => setLim('fallback', v)} />
          </Row>
          <Row label="Models" hint="what the worker may plan with" wide was={was('tiers', preset.tiers.join(', '))} onReset={() => setLim('tiers', null)}>
            <Chips label="Models the worker may use" value={val('tiers')} onChange={(v) => setLim('tiers', v)}
              options={(b.model_options?.length ? b.model_options : o.tiers).map((t) => ({ id: t, label: tierLabel(t, providers.data?.find((p) => p.id === b.provider)) }))} />
          </Row>
          {b.kind === 'claude-subscription' && (
            <Row label="Sweep" hint={`in the last ${sweepWindow} h before the reset, spend what you didn't use, up to a ceiling`} wide was={was('sweep', preset.sweep ? 'on' : 'off')} onReset={() => setLim('sweep', null)}>
              {val('sweep') && <>
                <NumberField label="Sweep window" value={sweepWindow} min={0.5} max={48} step={0.5} suffix="h before" onCommit={(v) => setRes('sweep_window_hours', v, o.sweep_window_hours)} width={64} />
                <NumberField label="Sweep ceiling" value={sweepCeiling} max={100} suffix="% of the week" onCommit={(v) => setRes('sweep_ceiling_pct', v, o.sweep_ceiling_pct)} width={64} />
              </>}
              <Switch label="Sweep" on={val('sweep')} onChange={(v) => setLim('sweep', v)} />
            </Row>
          )}
        </div>
        {dirty && (
          <div className="save-bar" role="group" aria-label="Unsaved changes">
            <span>Unsaved changes</span>
            <div className="btns">
              <button className="btn" onClick={discard} disabled={patch.isPending || reserve.isPending}>Discard</button>
              <button className="btn primary" onClick={save} disabled={patch.isPending || reserve.isPending}>Save</button>
            </div>
          </div>
        )}
        {changed.length > 0 && (
          <div className="sub modified-line">
            Modified from {L}'s defaults: {changed.join(', ')}. <button className="link-btn accent" onClick={resetAll}>Reset to default</button>
          </div>
        )}
      </section>

      <section className="set-section">
        <div className="set-title">Always, whatever the level</div>
        <div className="set-rows">
          <Row label="Reply budget" hint="for “Reply now”, separate from the allowance">
            <NumberField label="Reply budget" value={b.reply_allowance} suffix={`pts / ${per}`} onCommit={(v) => patch.mutate({ provider: b.provider, reply_pts: v }, done)} />
          </Row>
          {b.kind === 'claude-subscription' ? <>
            <Row label="Reserve for you · week" hint="no wakeup starts above this much of the week">
              <NumberField label="Weekly reserve" value={o.reserve_weekly_pct} max={100} suffix="%" onCommit={(v) => reserve.mutate({ reserve_weekly_pct: v }, done)} />
            </Row>
            <Row label="Reserve for you · 5 hours" hint="nor above this much of the 5-hour window">
              <NumberField label="5-hour reserve" value={o.reserve_five_hour_pct} max={100} suffix="%" onCommit={(v) => reserve.mutate({ reserve_five_hour_pct: v }, done)} />
            </Row>
          </> : <>
            <Row label="Budget period" hint="when the allowance resets">
              {b.period === 'month' && <NumberField label="Reset day" value={b.reset_day ?? 1} min={1} max={28} suffix="day" width={56} onCommit={(v) => patch.mutate({ provider: b.provider, reset_day: v }, done)} />}
              <Select variant="input" value={b.period ?? 'week'} label="Budget period"
                options={[{ value: 'week' as const, label: 'Weekly' }, { value: 'month' as const, label: 'Monthly' }]}
                onChange={(v) => patch.mutate({ provider: b.provider, period: v }, done)} />
            </Row>
            <Row label="Cap · any 5 hours" hint={`at most this share of the ${per}'s allowance (0 = off)`}>
              <NumberField label="5-hour cap" value={Math.round((b.caps?.five_hour ?? 0) * 100)} max={100} suffix="%" onCommit={(v) => patch.mutate({ provider: b.provider, caps: { five_hour: v / 100 || null, week: b.caps?.week ?? null } }, done)} />
            </Row>
            <Row label="Cap · any 7 days" hint="opencode Go's own limits are 20% and 50% of its month">
              <NumberField label="7-day cap" value={Math.round((b.caps?.week ?? 0) * 100)} max={100} suffix="%" onCommit={(v) => patch.mutate({ provider: b.provider, caps: { five_hour: b.caps?.five_hour ?? null, week: v / 100 || null } }, done)} />
            </Row>
          </>}
        </div>
      </section>

      <div className="boost-box">
        {b.boost ? (
          <>
            <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Boost: {cap(b.boost.level)}</span>
              <span className="sub" style={{ fontSize: 12.5 }}>Until {stamp(b.boost.until)}; then back to {L}{b.custom.length ? ' (with your changes)' : ''}.</span></div>
            <button className="btn" onClick={() => endBoost.mutate(b.provider, done)}>End boost</button>
          </>
        ) : (
          <>
            <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Boost</span>
              <span className="sub" style={{ fontSize: 12.5 }}>Use {cap(next)}'s limits for a while ({ptsPer(o.levels.find((l) => l.name === next)?.limits.allowance ?? 0)}), then come back to {L}. Your reserve still protects your own use.</span></div>
            <div className="btns">
              <button className="btn" disabled={next === b.level} onClick={() => boost.mutate({ provider: b.provider, level: next, until_reset: true }, done)}>Until the reset</button>
              <button className="btn primary" disabled={next === b.level} onClick={() => boost.mutate({ provider: b.provider, level: next, hours: 24 }, done)}>Boost for 24 h</button>
            </div>
          </>
        )}
      </div>

      <div className="boost-box">
        <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Extra room this {per}</span>
          <span className="sub" style={{ fontSize: 12.5 }}>
            {b.bonus_pts || b.spend_from
              ? <>{b.bonus_pts ? `+${Math.round(b.bonus_pts)} bonus pts` : 'No bonus points'}{b.spend_from ? `; the assistant's spend counts from ${stamp(b.spend_from)}` : ''}. Both end at the reset ({stamp(b.resets_at)}).</>
              : <>Used a usage reset, or want to give it more this {per}? Add bonus points, or start the assistant's count over. Both end at the reset; your reserve still applies.</>}
          </span></div>
        <div className="btns">
          <NumberField label="Bonus points" value={bonusPts} min={1} max={1000} suffix="pts" width={64} onCommit={setBonusPts} />
          <button className="btn" disabled={addBonus.isPending} onClick={() => addBonus.mutate({ provider: b.provider, pts: bonusPts }, done)}>Add</button>
          <button className="btn" disabled={addBonus.isPending} onClick={() => addBonus.mutate({ provider: b.provider, reset_spend: true }, done)}>Start its count over</button>
          {((b.bonus_pts ?? 0) > 0 || b.spend_from) && <button className="btn" disabled={clearBonus.isPending} onClick={() => clearBonus.mutate(b.provider, done)}>Take back</button>}
        </div>
      </div>
    </div>
  )
}

// ── Providers ────────────────────────────────────────────────────────────
type AddKind = 'claude-subscription' | 'anthropic-api' | 'opencode-go'
const ADDABLE: [AddKind, string, string][] = [
  ['claude-subscription', 'Claude subscription', 'Your Pro or Max plan; the assistant signs in on its own computer. Usage comes from the weekly and 5-hour meter.'],
  ['anthropic-api', 'Anthropic API key', 'Pay as you go; 1 pt per $0.10 of the runtime\'s reported cost.'],
  ['opencode-go', 'opencode Go', 'A subscription to open models, run with opencode instead of Claude Code; monthly limits.'],
]

/** Connected providers, with "+ Add provider" offering only the ones not connected yet. */
export function Providers() {
  const providers = useProviders()
  const sys = useSystem()
  const patch = usePatchProvider()
  const del = useDeleteProvider()
  const [adding, setAdding] = useState(false)
  const [relogin, setRelogin] = useState(false)
  const addBtn = useRef<HTMLButtonElement>(null)
  const loggedIn = !!sys.data?.runner.logged_in
  const signedOut = !!sys.data?.runner.login_problem  // a login Anthropic turned down: offer the sign-in right away
  const connected = (providers.data ?? []).filter((p) => p.kind !== 'claude-subscription' || loggedIn)
  const available = ADDABLE.filter(([k]) => !connected.some((p) => p.kind === k))
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row-between">
        <div className="set-title">Providers</div>
        {available.length > 0 && <button ref={addBtn} className="btn primary" onClick={() => setAdding(true)}>+ Add provider</button>}
      </div>
      <AddProvider open={adding} available={available} reachable={!!sys.data?.runner.reachable} close={() => { setAdding(false); addBtn.current?.focus() }} />
      {providers.isSuccess && !connected.length && <div className="empty">No provider connected yet. Add one so the assistant can run.</div>}
      {connected.map((p) => (
        <div key={p.id} className="provider-card">
          <div className="stack" style={{ gap: 2, minWidth: 0 }}>
            <span style={{ font: '500 14px var(--sans)' }}>{p.label} {!p.enabled && <span className="pill mute" style={{ marginLeft: 6 }}>off</span>}</span>
            <span className="sub" style={{ fontSize: 12.5, ...(p.kind === 'claude-subscription' && signedOut ? { color: 'var(--badInk)' } : {}) }}>{p.status}</span>
          </div>
          <div className="btns">
            {p.kind !== 'claude-subscription' && <ReplaceKey p={p} />}
            {p.kind === 'claude-subscription' && !signedOut && <button className="btn small" aria-expanded={relogin} onClick={() => setRelogin(!relogin)}>{relogin ? 'Cancel' : 'Sign in again'}</button>}
            <button className="btn small" onClick={() => patch.mutate({ id: p.id, enabled: !p.enabled })}>{p.enabled ? 'Turn off' : 'Turn on'}</button>
            {p.kind !== 'claude-subscription' && <button className="btn small danger" onClick={() => del.mutate(p.id)}>Remove</button>}
          </div>
          {p.kind === 'claude-subscription' && (relogin || signedOut) && (
            <div className="stack" style={{ gap: 8, flexBasis: '100%' }}>
              <span className="sub" style={{ fontSize: 12.5 }}>Signs the assistant's computer in again (or into a different Claude account); the new login replaces the old one.</span>
              {sys.data?.runner.reachable ? <ClaudeLogin /> : <span className="sub">The assistant's computer isn't reachable yet; try again when it is.</span>}
            </div>
          )}
          {p.models && <ModelList provider={p} />}
        </div>
      ))}
      {patch.error && <div className="error" role="alert">{problem(patch.error)}</div>}
      {connected.length > 0 && <div className="sub" style={{ fontSize: 12.5 }}>Keys are write-only: stored encrypted and given only to sessions on that provider. Choose which jobs run where under Roles.</div>}
    </div>
  )
}

/** "+ Add provider": a popup with a search over the providers you can add; picking one shows
 * its key form (or the Claude sign-in) in the popup. Esc or a click outside closes it. */
function AddProvider({ open, available, reachable, close }: { open: boolean; available: [AddKind, string, string][]; reachable: boolean; close: () => void }) {
  const [q, setQ] = useState('')
  const [pick, setPickRaw] = useState<AddKind | null>(null)
  const [dir, setDir] = useState<'fwd' | 'back'>('fwd')
  const setPick = (k: AddKind | null) => { setDir(k ? 'fwd' : 'back'); setPickRaw(k) }
  const { mounted, closing } = usePresence(open)
  useEscape(open, () => (pick ? setPick(null) : close()))
  useEffect(() => { if (open) { setPickRaw(null); setQ('') } }, [open])
  if (!mounted) return null
  const shown = available.filter(([, label, text]) => !q.trim() || `${label} ${text}`.toLowerCase().includes(q.trim().toLowerCase()))
  const chosen = available.find(([k]) => k === pick)
  // A portal: the page's entrance animation would otherwise contain this fixed popup.
  return createPortal(
    <div className={`scrim ${closing ? 'closing' : ''}`} onClick={close}>
      <div className="palette add-provider" role="dialog" aria-modal="true" aria-label="Add a provider" onClick={(e) => e.stopPropagation()}>
        {!chosen ? <div key="list" className={`stack ${dir === 'back' ? 'view-back' : ''}`} style={{ gap: 0, minHeight: 0 }}>
          <input className="palette-input" autoFocus placeholder="Search providers" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search providers"
            onKeyDown={(e) => { if (e.key === 'Enter' && shown[0]) setPick(shown[0][0]) }} />
          <div className="ap-list">
            {shown.map(([k, label, text]) => (
              <button key={k} className="ap-row" onClick={() => setPick(k)}>
                <span className="stack" style={{ gap: 2, minWidth: 0 }}><span className="t">{label}</span><span className="sub">{text}</span></span>
                <span style={{ color: 'var(--faint)' }}>›</span>
              </button>
            ))}
            {!shown.length && <div className="sub" style={{ padding: 16 }}>No provider matches “{q}”. More providers (Google AI Studio, OpenRouter, local models) come later.</div>}
          </div>
        </div> : (
          <div key={chosen[0]} className="stack view-fwd" style={{ gap: 12, padding: 18 }}>
            <div className="row-between"><span style={{ font: '500 15px var(--sans)' }}>{chosen[1]}</span><button className="link-btn" onClick={() => setPick(null)}>← All providers</button></div>
            <span className="sub" style={{ fontSize: 12.5 }}>{chosen[2]}</span>
            {chosen[0] === 'claude-subscription'
              ? (reachable ? <ClaudeLogin /> : <span className="sub">The assistant's computer isn't reachable yet; try again when it is.</span>)
              : <KeyForm kind={chosen[0]} done={close} />}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

function KeyForm({ kind, done }: { kind: 'anthropic-api' | 'opencode-go'; done: () => void }) {
  const add = useAddApiProvider()
  const [key, setKey] = useState('')
  return (
    <form className="set-form" onSubmit={(e) => { e.preventDefault(); add.mutate({ key: key.trim(), label: null, kind, use_for_all_roles: false }, { onSuccess: () => { setKey(''); done() } }) }}>
      <span className="sub" style={{ fontSize: 12.5 }}>{kind === 'opencode-go' ? 'Paste the key from opencode.ai → your workspace → API keys.' : 'Paste an Anthropic API key (it starts with sk-ant-).'}</span>
      <input className="input" type="password" autoComplete="off" autoFocus placeholder={kind === 'opencode-go' ? 'opencode API key' : 'sk-ant-…'} value={key} onChange={(e) => setKey(e.target.value)} aria-label={kind === 'opencode-go' ? 'opencode Go key' : 'API key'} />
      {add.error && <div className="error" role="alert">{problem(add.error)}</div>}
      <div><button className="btn primary" disabled={key.trim().length < 20 || add.isPending}>Connect</button></div>
    </form>
  )
}

function ReplaceKey({ p }: { p: S['ProviderInfo'] }) {
  const add = useAddApiProvider()
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState('')
  if (!open) return <button className="btn small" onClick={() => setOpen(true)}>Replace key</button>
  return (
    <form className="inline-edit" onSubmit={(e) => { e.preventDefault(); add.mutate({ key: key.trim(), label: null, kind: p.kind as 'anthropic-api' | 'opencode-go', use_for_all_roles: false }, { onSuccess: () => { setKey(''); setOpen(false) } }) }}>
      <input className="input" type="password" autoComplete="off" autoFocus value={key} onChange={(e) => setKey(e.target.value)} placeholder="New key" aria-label={`New key for ${p.label}`} />
      <button className="btn small primary" disabled={key.trim().length < 20 || add.isPending}>Save</button>
      <button type="button" className="btn small" onClick={() => setOpen(false)}>Cancel</button>
    </form>
  )
}

/** An opencode provider's models: the ones the assistant may use, and the catalog to add from. */
function ModelList({ provider }: { provider: S['ProviderInfo'] }) {
  const patch = usePatchProvider()
  const [adding, setAdding] = useState(false)
  const [q, setQ] = useState('')
  const catalog = useProviderCatalog(provider.id, adding)
  const ids = (provider.models ?? []).map((m) => m.id)
  const save = (next: string[]) => patch.mutate({ id: provider.id, models: next })
  const move = (i: number, d: number) => { const n = [...ids]; [n[i], n[i + d]] = [n[i + d], n[i]]; save(n) }
  const shown = (catalog.data ?? []).filter((m) => !q.trim() || `${m.id} ${m.name}`.toLowerCase().includes(q.trim().toLowerCase()))
  return (
    <div className="stack" style={{ gap: 8, gridColumn: '1 / -1', marginTop: 8 }}>
      <span className="sub" style={{ fontSize: 12.5 }}>Models the assistant may use (roles, chats and wakeups pick from these; the first is the default):</span>
      <div className="model-list">
        {modelChoices(provider).map((m, i) => (
          <div key={m.id} className="model-row">
            <div className="stack" style={{ gap: 1, minWidth: 0 }}>
              <span style={{ font: '500 13px var(--sans)' }}>{m.label}</span>
              <span className="sub mono" style={{ fontSize: 11.5 }}>{m.id}{m.efforts.length ? ` · efforts ${m.efforts.join(', ')}` : ''}</span>
            </div>
            <span className="sub" style={{ fontSize: 12 }}>{m.price}</span>
            <div className="btns" style={{ gap: 2 }}>
              <button className="icon-btn" disabled={i === 0 || patch.isPending} onClick={() => move(i, -1)} aria-label={`Move ${m.label} up`}>↑</button>
              <button className="icon-btn" disabled={i === ids.length - 1 || patch.isPending} onClick={() => move(i, 1)} aria-label={`Move ${m.label} down`}>↓</button>
              <button className="icon-btn" disabled={ids.length <= 1 || patch.isPending} onClick={() => save(ids.filter((x) => x !== m.id))} aria-label={`Remove ${m.label}`}>✕</button>
            </div>
          </div>
        ))}
      </div>
      {!adding ? <div><button className="btn small" onClick={() => setAdding(true)}>+ Add models</button></div> : (
        <div className="stack" style={{ gap: 6 }}>
          <div className="row-start"><input className="input" placeholder="Search the catalog" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search models" style={{ maxWidth: 280 }} />
            <button className="btn small" onClick={() => setAdding(false)}>Done</button></div>
          {catalog.isLoading && <span className="sub">Loading the catalog…</span>}
          <div className="model-list" style={{ maxHeight: 320, overflow: 'auto' }}>
            {shown.map((m) => (
              <label key={m.id} className="model-row" style={{ cursor: 'pointer' }}>
                <span className="row-start" style={{ gap: 8, minWidth: 0 }}>
                  <input type="checkbox" checked={ids.includes(m.id)} disabled={patch.isPending || (ids.length <= 1 && ids.includes(m.id))}
                    onChange={(e) => save(e.target.checked ? [...ids, m.id] : ids.filter((x) => x !== m.id))} />
                  <span className="stack" style={{ gap: 1, minWidth: 0 }}><span style={{ fontSize: 13 }}>{m.name}</span><span className="sub mono" style={{ fontSize: 11.5 }}>{m.id}</span></span>
                </span>
                <span className="sub" style={{ fontSize: 12 }}>{m.input === 0 && m.output === 0 ? 'free' : `$${m.input ?? '?'} / $${m.output ?? '?'} per M`}</span>
              </label>
            ))}
          </div>
        </div>
      )}
      {patch.error && <div className="error" role="alert">{problem(patch.error)}</div>}
    </div>
  )
}

// ── Roles ────────────────────────────────────────────────────────────────
export function Roles() {
  const roles = useRoles()
  const providers = useProviders()
  const set = useSetRoles()
  const [savedAt, setSavedAt] = useState(0)
  if (!roles.data) return null
  const save = (role: string, provider: string, tier: string) =>
    set.mutate({ roles: roles.data!.roles.map((r) => (r.role === role ? { ...r, provider, tier } : r)) }, { onSuccess: () => setSavedAt(Date.now()) })
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="set-head"><div className="sub" style={{ fontSize: 13 }}>Which provider and model each job uses. Changes save right away.</div><Saved token={savedAt} error={set.error ? problem(set.error) : null} /></div>
      <div className="card-flat">
        <div className="roles-row head"><span>Role</span><span>Model</span><span>What it does</span></div>
        {roles.data.roles.map((r) => {
          const prov = providers.data?.find((p) => p.id === r.provider)
          return (
            <div key={r.role} className="roles-row">
              <span style={{ fontWeight: 500 }}>{cap(r.role)}</span>
              <ModelPicker label={`${r.role} model`} providers={providers.data ?? []} provider={r.provider} tier={r.tier} onChange={(p, t) => save(r.role, p, t)} />
              <span className="sub" style={{ fontSize: 12.5 }}>{r.role === 'worker' ? `${r.note} Allowed now: ${r.allowed.map((t) => tierLabel(t, prov)).join(', ') || 'none'}.` : r.note}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Reviewer ─────────────────────────────────────────────────────────────
type RC = Required<S['ReviewerConfig']>
const MODES: [RC['mode'], string][] = [['off', 'Off'], ['advisor', 'Advisor'], ['monitor', 'Monitor'], ['gate', 'Gate']]
// Each stop does everything the one before it does, plus one thing.
const DOES: [number, string][] = [
  [1, 'Answers when the assistant asks it something'],
  [2, 'Reads its notes as it works, and may interject'],
  [3, 'Must agree before it ends a wakeup or closes a task as done'],
]

/** Off → Advisor → Monitor → Gate as one slider with four locked stops. */
function ModeSlider({ value, onChange }: { value: RC['mode']; onChange: (m: RC['mode']) => void }) {
  const i = Math.max(0, MODES.findIndex(([m]) => m === value))
  const track = useRef<HTMLDivElement>(null)
  const at = (clientX: number) => {
    const r = track.current!.getBoundingClientRect()
    return Math.round(Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * (MODES.length - 1))
  }
  const go = (n: number) => { if (n !== i && n >= 0 && n < MODES.length) onChange(MODES[n][0]) }
  return (
    <div className="mode-slider">
      <div ref={track} className="ms-track" role="slider" tabIndex={0} aria-label="Reviewer mode" aria-valuemin={0} aria-valuemax={3} aria-valuenow={i} aria-valuetext={MODES[i][1]}
        onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture?.(e.pointerId); go(at(e.clientX)) }}
        onPointerMove={(e) => { if (e.buttons) go(at(e.clientX)) }}
        onKeyDown={(e) => { if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); go(i + 1) } if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); go(i - 1) } }}>
        <div className="ms-rail" />
        <div className="ms-fill" style={{ width: `${(i / (MODES.length - 1)) * 100}%` }} />
        {MODES.map(([m], n) => <span key={m} className={`ms-stop ${n <= i ? 'on' : ''}`} style={{ left: `${(n / (MODES.length - 1)) * 100}%` }} />)}
        <span className="ms-knob" style={{ left: `${(i / (MODES.length - 1)) * 100}%` }} />
      </div>
      <div className="ms-labels">
        {MODES.map(([m, label], n) => <button key={m} className={n === i ? 'on' : ''} style={{ left: `${(n / (MODES.length - 1)) * 100}%` }} onClick={() => go(n)}>{label}</button>)}
      </div>
      <ul className="ms-does">
        {i === 0 && <li className="off">Wakeups run without a reviewer.</li>}
        {DOES.map(([n, text]) => <li key={n} className={n <= i ? 'yes' : 'no'}><span className="mark">{n <= i ? '✓' : ''}</span>{text}<span className="from">{n > i ? `from ${MODES[n][1]}` : ''}</span></li>)}
      </ul>
    </div>
  )
}

export function Reviewer() {
  const rv = useReviewer()
  const set = useSetReviewer()
  const providers = useProviders()
  const roles = useRoles()
  const budget = useBudget()
  const [c, setC] = useState<RC | null>(null)
  const [savedAt, setSavedAt] = useState(0)
  useEffect(() => { if (rv.data) setC(rv.data as RC) }, [rv.data])
  if (!c) return null
  const save = (upd: Partial<RC>) => { const n = { ...c, ...upd }; setC(n); set.mutate(n, { onSuccess: () => setSavedAt(Date.now()) }) }
  const worker = roles.data?.roles.find((r) => r.role === 'worker')
  const wprov = providers.data?.find((p) => p.id === worker?.provider)
  const wb = budget.data?.providers.find((p) => p.provider === worker?.provider)
  const options = (wb?.model_options?.length ? wb.model_options : budget.data?.tiers ?? c.tier_choices)
  return (
    <div className="stack" style={{ gap: 28 }}>
      <section className="set-section">
        <div className="set-head"><div className="set-title">Mode</div><Saved token={savedAt} error={set.error ? problem(set.error) : null} /></div>
        <ModeSlider value={c.mode} onChange={(m) => save({ mode: m })} />
        <div className="sub" style={{ fontSize: 12.5 }}>The reviewer is a second model that watches wakeups and routines (not quick replies). It reads the assistant's memory but can't change anything.</div>
      </section>
      <section className="set-section">
        <div className="set-title">The reviewer</div>
        <div className="set-rows">
          <Row label="Model" hint="usually stronger than the worker's" wide>
            <ModelPicker label="Reviewer model" providers={providers.data ?? []} provider={c.provider} tier={c.tier} onChange={(p, t) => save({ provider: p, tier: t })} />
          </Row>
          <Row label="Watch interval" hint="how often it reads new notes (Monitor and Gate)">
            <NumberField label="Watch interval" value={c.interval_minutes} min={1} suffix="min" onCommit={(v) => save({ interval_minutes: v })} />
          </Row>
          <Row label="Budget share" hint="the most of a wakeup's budget it may use">
            <NumberField label="Budget share" value={Math.round(c.budget_share * 100)} min={1} max={100} suffix="%" onCommit={(v) => save({ budget_share: v / 100 })} />
          </Row>
          <Row label="One conversation per" hint="it remembers what it said within one" wide>
            <Select variant="input" value={c.continuity} label="Reviewer continuity"
              options={[{ value: 'day' as const, label: 'day' }, { value: 'wakeup' as const, label: 'wakeup' }]}
              onChange={(v) => save({ continuity: v })} />
          </Row>
        </div>
      </section>
      <section className="set-section">
        <div className="set-title">Changing the worker's model</div>
        <div className="sub" style={{ fontSize: 12.5, marginTop: -4 }}>
          The reviewer can move the worker to another model mid-wakeup, for example a stronger one for a hard step. The worker resumes the same conversation on it.
        </div>
        <div className="set-rows one">
          <Row label="Let the reviewer change the worker's model">
            <Switch label="Let the reviewer change the worker's model" on={c.change_tier} onChange={(v) => save({ change_tier: v })} />
          </Row>
          {c.change_tier && <>
            <Row label="Changes per wakeup" hint="at most; each one rereads the whole conversation, so keep it low">
              <NumberField label="Changes per wakeup" value={c.max_tier_changes} min={1} max={20} onCommit={(v) => save({ max_tier_changes: v })} />
            </Row>
            <Row label="It may switch to" hint={`models on ${wprov?.label ?? 'the worker\'s provider'}`} wide>
              <Chips label="Models the reviewer may switch to" value={c.tier_choices} onChange={(v) => save({ tier_choices: v })}
                options={options.map((t) => ({ id: t, label: tierLabel(t, wprov) }))} />
            </Row>
          </>}
        </div>
      </section>
    </div>
  )
}

// ── Standing instructions ────────────────────────────────────────────────
export function Instructions() {
  useTimeFormat()
  const ins = useInstructions()
  const set = useSetInstructions()
  const [t, setT] = useState<string | null>(null)
  const [r, setR] = useState('')
  const [savedAt, setSavedAt] = useState(0)
  useEffect(() => { if (ins.data && t === null) { setT(ins.data.text); setR(ins.data.reviewer_text ?? '') } }, [ins.data, t])
  if (t === null) return null
  // Either box saves both, so neither is wiped by a stale copy.
  const commit = (text: string, reviewer: string) => {
    if (text !== ins.data?.text || reviewer !== (ins.data?.reviewer_text ?? '')) set.mutate({ text, reviewer_text: reviewer }, { onSuccess: () => setSavedAt(Date.now()) })
  }
  return (
    <>
      <section className="set-section">
        <div className="set-head"><div className="set-title">Standing instructions</div><Saved token={savedAt} error={set.error ? problem(set.error) : null} /></div>
        <div className="sub" style={{ fontSize: 13 }}>What the assistant should always keep in mind. They open every brief; it hears when you change them. Saved when you leave the box.</div>
        <textarea className="textarea" rows={10} value={t} onChange={(e) => setT(e.target.value)} onBlur={() => commit(t, r)} aria-label="Standing instructions" maxLength={8000} />
        <span className="sub" style={{ fontSize: 12 }}>{ins.data?.updated_at ? `Last changed ${stamp(ins.data.updated_at)}` : 'Not set yet'}</span>
      </section>
      <section className="set-section">
        <div className="set-title">For the reviewer only</div>
        <div className="sub" style={{ fontSize: 13 }}>The reviewer reads these; the assistant doesn't.</div>
        <textarea className="textarea" rows={6} value={r} onChange={(e) => setR(e.target.value)} onBlur={() => commit(t, r)} aria-label="Instructions for the reviewer only" maxLength={8000} />
      </section>
    </>
  )
}
