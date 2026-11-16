import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useCalendar, useCancelWakeup, useCreateRoutine, useCreateWakeup, useDeleteRoutine, usePatchRoutine, usePatchWakeup, useRoutines,
} from '../api/queries'

// The week grid from the mockup: 28 px an hour, protected hours hatched, wakeups colored by
// kind. Planned wakeups can be dragged to another time or day (15-min steps) or opened to
// change their budget or cancel them; the side panel shows the budget, what's scheduled,
// you vs. the assistant, and routines.
const H = 28
const DAY_H = 24 * H
const KIND_CLASS: Record<S['Wakeup']['kind'], string> = { planned: 'wakeup', fallback: 'fallback', sweep: 'sweep', routine: 'routine', owner: 'mine' }
const LEGEND: [string, string][] = [['Planned wakeup', 'wakeup'], ['Routine', 'routine'], ['Sweep', 'sweep'], ['Fallback', 'fallback'], ['Yours', 'mine'], ['Protected hours', 'protected']]

/** Wall-clock parts of an instant in a time zone. */
function parts(iso: string | Date, tz: string) {
  const d = typeof iso === 'string' ? new Date(iso) : iso
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short' })
  const p = Object.fromEntries(f.formatToParts(d).map((x) => [x.type, x.value]))
  return { date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour), minute: Number(p.minute), weekday: p.weekday, day: Number(p.day) }
}

/** The UTC instant for a wall-clock time in a time zone. */
function zonedToUtc(date: string, minutes: number, tz: string): Date {
  const [y, m, d] = date.split('-').map(Number)
  const guess = new Date(Date.UTC(y, m - 1, d, Math.floor(minutes / 60), minutes % 60))
  const p = parts(guess, tz)
  const shown = Date.UTC(Number(p.date.slice(0, 4)), Number(p.date.slice(5, 7)) - 1, Number(p.date.slice(8, 10)), p.hour, p.minute)
  return new Date(guess.getTime() - (shown - guess.getTime()))
}

const fmtDay = (iso: string, tz: string) => new Date(iso).toLocaleDateString(undefined, { timeZone: tz, month: 'short', day: 'numeric' })
const when = (iso: string, tz: string) => new Date(iso).toLocaleString(undefined, { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit' })
const tierText = (t: string) => t.split('/').map((x) => x[0].toUpperCase() + x.slice(1)).join(' · ')

export function Calendar() {
  const [offset, setOffset] = useState(0)
  const [start, end] = useMemo(() => {
    if (!offset) return [undefined, undefined]
    const now = new Date()
    const monday = new Date(now)
    monday.setDate(now.getDate() - ((now.getDay() + 6) % 7) + offset * 7)
    monday.setHours(0, 0, 0, 0)
    return [monday.toISOString(), new Date(monday.getTime() + 7 * 86400_000).toISOString()]
  }, [offset])
  const cal = useCalendar(start, end)
  const [open, setOpen] = useState<number | 'new' | null>(null)
  if (cal.isError) return <div className="page"><div className="error">Couldn't load the calendar.</div></div>
  const c = cal.data
  if (!c) return <div className="page" />
  const tz = c.timezone
  const days = Array.from({ length: 7 }, (_, i) => {
    const d = new Date(new Date(c.start).getTime() + i * 86400_000 + 12 * 3600_000)
    return parts(d, tz)
  })
  const today = parts(new Date(), tz).date
  const selected = typeof open === 'number' ? c.wakeups.find((w) => w.id === open) ?? null : null
  return (
    <div className="calendar">
      <div className="row-between" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <h1 className="serif-title">Calendar</h1>
          <span className="sub" style={{ fontSize: 14 }}>{fmtDay(c.start, tz)} – {fmtDay(new Date(new Date(c.end).getTime() - 1).toISOString(), tz)}, {new Date(c.start).getFullYear()}</span>
        </div>
        <div className="btns">
          <button className="btn" aria-label="Previous week" onClick={() => setOffset(offset - 1)}>‹</button>
          <button className="btn" onClick={() => setOffset(0)}>This week</button>
          <button className="btn" aria-label="Next week" onClick={() => setOffset(offset + 1)}>›</button>
          <button className="btn primary" onClick={() => setOpen('new')}>+ Add wakeup</button>
        </div>
      </div>
      <div className="cal-legend">{LEGEND.map(([label, k]) => <span key={k}><span className={`cal-swatch ${k}`} />{label}</span>)}</div>
      <div className="cal-layout">
        <Week c={c} days={days} today={today} onOpen={setOpen} />
        <Side c={c} />
      </div>
      {open === 'new' && <WakeupEditor tz={tz} tiers={c.budget.limits.tiers} onClose={() => setOpen(null)} />}
      {selected && <WakeupEditor tz={tz} w={selected} tiers={c.budget.limits.tiers} onClose={() => setOpen(null)} />}
    </div>
  )
}

type Day = ReturnType<typeof parts>

function Week({ c, days, today, onOpen }: { c: S['Calendar']; days: Day[]; today: string; onOpen: (id: number) => void }) {
  const tz = c.timezone
  const patch = usePatchWakeup()
  const grid = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<{ id: number; dx: number; dy: number; x0: number; y0: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Allowed windows per day as [startMin, endMin]; the rest is protected for you.
  const allowed = days.map((d) => {
    const out: [number, number][] = []
    for (const w of c.windows.filter((x) => x.kind === 'allowed')) {
      const a = parts(w.start, tz), b = parts(w.end, tz)
      const s0 = a.date < d.date ? 0 : a.date === d.date ? a.hour * 60 + a.minute : null
      const e0 = b.date > d.date ? 1440 : b.date === d.date ? b.hour * 60 + b.minute : null
      if (s0 != null && e0 != null && e0 > s0) out.push([s0, e0])
    }
    return out.sort((x, y) => x[0] - y[0])
  })
  const protectedBlocks = allowed.map((xs) => {
    const out: [number, number][] = []
    let t = 0
    for (const [a, b] of xs) {
      if (a > t) out.push([t, a])
      t = Math.max(t, b)
    }
    if (t < 1440) out.push([t, 1440])
    return out
  })
  const sweep = c.windows.find((w) => w.kind === 'sweep')

  function drop(e: React.PointerEvent, w: S['Wakeup']) {
    if (!drag || !grid.current) return
    const moved = Math.abs(e.clientX - drag.x0) + Math.abs(e.clientY - drag.y0)
    setDrag(null)
    if (moved < 4) return onOpen(w.id)
    const cols = Array.from(grid.current.querySelectorAll<HTMLElement>('[data-day]'))
    const col = cols.find((el) => { const r = el.getBoundingClientRect(); return e.clientX >= r.left && e.clientX < r.right })
    if (!col) return
    const r = col.getBoundingClientRect()
    const minutes = Math.max(0, Math.min(1440 - 15, Math.round(((e.clientY - r.top - drag.dy) / H) * 4) * 15))
    const at = zonedToUtc(col.dataset.day!, minutes, tz).toISOString()
    patch.mutate({ id: w.id, at }, { onError: (x) => setErr(problem(x)), onSuccess: () => setErr(null) })
  }

  return (
    <div className="cal-grid-card" ref={grid}>
      {err && <div className="error" style={{ margin: 8 }}>{err}</div>}
      <div className="cal-head">
        <div />
        {days.map((d) => <div key={d.date} className={d.date === today ? 'today' : ''}><span>{d.weekday}</span><span style={{ fontWeight: 400 }}>{d.day}</span></div>)}
      </div>
      <div className="cal-body">
        <div className="cal-hours" style={{ height: DAY_H }}>
          {[0, 3, 6, 9, 12, 15, 18, 21].map((h) => <span key={h} style={{ top: h * H + 2 }}>{String(h).padStart(2, '0')}</span>)}
        </div>
        {days.map((d, i) => (
          <div key={d.date} data-day={d.date} className={`cal-day ${d.date === today ? 'today' : ''}`} style={{ height: DAY_H }}>
            {protectedBlocks[i].map(([a, b]) => <div key={a} className="cal-ev protected" style={{ top: (a / 60) * H, height: Math.max(16, ((b - a) / 60) * H - 2) }}><span>Protected</span></div>)}
            {sweep && (() => {
              const a = parts(sweep.start, tz), b = parts(sweep.end, tz)
              if (a.date > d.date || b.date < d.date) return null
              const s0 = a.date === d.date ? a.hour * 60 + a.minute : 0, e0 = b.date === d.date ? b.hour * 60 + b.minute : 1440
              return e0 > s0 ? <div className="cal-ev sweep-window" style={{ top: (s0 / 60) * H, height: ((e0 - s0) / 60) * H - 2 }} title="Sweep window: before the weekly reset" /> : null
            })()}
            {c.wakeups.filter((w) => parts(w.at, tz).date === d.date).map((w) => {
              const p = parts(w.at, tz)
              const top = ((p.hour * 60 + p.minute) / 60) * H
              const hours = Math.min(3, Math.max(0.5, w.budget / 20))
              const isDrag = drag?.id === w.id
              const editable = w.status === 'planned'
              return (
                <div key={w.id} role="button" tabIndex={0} title={`${w.reason} · ${w.budget} pts · ${w.tier}${w.status !== 'planned' ? ` · ${w.status}` : ''}`}
                  className={`cal-ev ${KIND_CLASS[w.kind]} ${w.status !== 'planned' ? 'past' : ''}`}
                  style={{ top, height: Math.max(16, hours * H - 2), transform: isDrag ? `translate(${drag.dx}px, ${drag.dy}px)` : undefined, zIndex: isDrag ? 5 : undefined, cursor: editable ? 'grab' : 'pointer' }}
                  onKeyDown={(e) => { if (e.key === 'Enter') onOpen(w.id) }}
                  onPointerDown={(e) => {
                    if (!editable) return onOpen(w.id)
                    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
                    setDrag({ id: w.id, dx: 0, dy: 0, x0: e.clientX, y0: e.clientY })
                  }}
                  onPointerMove={(e) => isDrag && setDrag({ ...drag, dx: e.clientX - drag.x0, dy: e.clientY - drag.y0 })}
                  onPointerUp={(e) => isDrag && drop(e, w)}>
                  <span>{w.kind === 'routine' ? w.reason.replace(/^Routine: /, '') : w.kind === 'fallback' ? 'Fallback' : w.kind === 'sweep' ? 'Sweep' : w.reason}</span>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

function Side({ c }: { c: S['Calendar'] }) {
  const b = c.budget
  const tz = c.timezone
  const scheduled = c.wakeups.filter((w) => w.status === 'planned').slice(0, 6)
  const you = b.used_by_owner + b.used_by_chat
  const total = Math.max(1, you + b.used_by_assistant)
  return (
    <div className="cal-side">
      <div className="stack" style={{ gap: 8 }}>
        <div className="side-label">Budget left this {b.period ?? 'week'}</div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}><span className="serif-num">{Math.round(b.left)}</span><span className="sub">of {Math.round(b.allowance)} pts</span></div>
        <div className="gauge"><div style={{ width: `${b.allowance ? Math.min(100, (b.left / b.allowance) * 100) : 0}%` }} /></div>
        <div className="sub" style={{ fontSize: 12 }}>{b.pace}</div>
      </div>
      <div className="stack" style={{ gap: 8 }}>
        <div className="side-label">Scheduled</div>
        {!scheduled.length && <div className="sub" style={{ fontSize: 13 }}>Nothing planned{b.limits.fallback ? '; control adds a fallback wakeup if nothing is planned for two nights' : ''}.</div>}
        {scheduled.map((w) => (
          <div key={w.id} className="cal-sched">
            <div className="row-between" style={{ font: '500 13px var(--sans)' }}><span>{when(w.at, tz)}</span><span className="sub">{w.budget} pts</span></div>
            <span className="sub" style={{ fontSize: 12.5, lineHeight: 1.4 }}>{w.reason}</span>
            <span className="mono" style={{ fontSize: 11.5, color: 'var(--faint2)' }}>{tierText(w.tier)}</span>
          </div>
        ))}
      </div>
      <div className="stack" style={{ gap: 8 }}>
        <div className="side-label">You vs. the assistant</div>
        <div className="split-bar"><div style={{ width: `${(you / total) * 100}%`, background: '#6b8fb8' }} /><div style={{ flex: 1, background: '#c4673f' }} /></div>
        <div className="row-between sub" style={{ fontSize: 12.5 }}><span>You · {Math.round(you)} pts</span><span>Assistant · {Math.round(b.used_by_assistant)} pts</span></div>
        {b.used_by_chat > 0 && <div className="sub" style={{ fontSize: 12 }}>Yours includes {Math.round(b.used_by_chat)} pts of 1:1 chats.</div>}
      </div>
      <Routines />
    </div>
  )
}

function Routines() {
  const routines = useRoutines()
  const patch = usePatchRoutine()
  const del = useDeleteRoutine()
  const create = useCreateRoutine()
  const [adding, setAdding] = useState(false)
  const [f, setF] = useState({ name: '', prompt: '', kind: 'daily', day: 0, time: '07:00', hours: 6, tier: 'sonnet/medium', budget: 8 })
  const [err, setErr] = useState<string | null>(null)
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row-between"><div className="side-label">Routines</div><button className="link-btn" onClick={() => setAdding(!adding)}>{adding ? 'Cancel' : '+ New'}</button></div>
      {routines.data?.map((r) => (
        <div key={r.id} className="cal-sched">
          <div className="row-between" style={{ font: '500 13px var(--sans)' }}>
            <span>{r.name}{r.status === 'proposed' ? ' (proposed)' : ''}</span>
            <label className="switch-label"><input type="checkbox" checked={r.enabled} onChange={(e) => patch.mutate({ id: r.id, enabled: e.target.checked })} aria-label={`${r.name} on`} /></label>
          </div>
          <span className="sub" style={{ fontSize: 12.5 }}>{r.schedule_text} · {r.budget} pts · {tierText(r.tier)}{r.created_by === 'assistant' ? ' · proposed by the assistant' : ''}</span>
          {!r.builtin && <button className="link-btn" style={{ alignSelf: 'flex-start', fontSize: 12 }} onClick={() => del.mutate(r.id)}>Delete</button>}
        </div>
      ))}
      {adding && (
        <form className="stack" style={{ gap: 6 }} onSubmit={(e) => {
          e.preventDefault()
          const schedule = f.kind === 'weekly' ? { kind: 'weekly', day: f.day, time: f.time } : f.kind === 'daily' ? { kind: 'daily', time: f.time } : { kind: 'before_reset', hours: f.hours }
          create.mutate({ name: f.name, prompt: f.prompt, schedule, tier: f.tier, budget: f.budget, enabled: true }, { onSuccess: () => { setAdding(false); setErr(null) }, onError: (x) => setErr(problem(x)) })
        }}>
          <input className="input" placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required />
          <textarea className="textarea" placeholder="What the assistant does each time" rows={3} value={f.prompt} onChange={(e) => setF({ ...f, prompt: e.target.value })} required />
          <div style={{ display: 'flex', gap: 6 }}>
            <select className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} aria-label="Schedule">
              <option value="daily">Daily</option><option value="weekly">Weekly</option><option value="before_reset">Before the reset</option>
            </select>
            {f.kind === 'weekly' && <select className="input" value={f.day} onChange={(e) => setF({ ...f, day: Number(e.target.value) })} aria-label="Day">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d, i) => <option key={d} value={i}>{d}</option>)}</select>}
            {f.kind === 'before_reset'
              ? <input className="input" type="number" min={0.5} max={72} step={0.5} value={f.hours} onChange={(e) => setF({ ...f, hours: Number(e.target.value) })} aria-label="Hours before" />
              : <input className="input" type="time" value={f.time} onChange={(e) => setF({ ...f, time: e.target.value })} aria-label="Time" />}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <input className="input" value={f.tier} onChange={(e) => setF({ ...f, tier: e.target.value })} aria-label="Tier" />
            <input className="input" type="number" min={1} value={f.budget} onChange={(e) => setF({ ...f, budget: Number(e.target.value) })} aria-label="Budget (pts)" />
          </div>
          {err && <div className="error">{err}</div>}
          <button className="btn primary small" disabled={create.isPending}>Add routine</button>
        </form>
      )}
    </div>
  )
}

function WakeupEditor({ w, tz, tiers, onClose }: { w?: S['Wakeup']; tz: string; tiers: string[]; onClose: () => void }) {
  const local = w ? parts(w.at, tz) : parts(new Date(Date.now() + 3600_000), tz)
  const [date, setDate] = useState(local.date)
  const [time, setTime] = useState(`${String(local.hour).padStart(2, '0')}:${String(local.minute).padStart(2, '0')}`)
  const [budget, setBudget] = useState(w?.budget ?? 10)
  const [tier, setTier] = useState(w?.tier ?? tiers[0] ?? 'sonnet/medium')
  const [reason, setReason] = useState(w?.reason ?? '')
  const [err, setErr] = useState<string | null>(null)
  const create = useCreateWakeup()
  const patch = usePatchWakeup()
  const cancel = useCancelWakeup()
  const editable = !w || w.status === 'planned'
  const save = () => {
    const [h, m] = time.split(':').map(Number)
    const at = zonedToUtc(date, h * 60 + m, tz).toISOString()
    const done = { onSuccess: onClose, onError: (x: unknown) => setErr(problem(x)) }
    if (w) patch.mutate({ id: w.id, at, budget, tier, reason }, done)
    else create.mutate({ at, budget, tier, reason: reason || 'Added by you' }, done)
  }
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="cal-editor card" role="dialog" aria-label={w ? 'Wakeup' : 'Add wakeup'}>
        <div className="row-between"><div style={{ font: '500 15px var(--sans)' }}>{w ? `Wakeup #${w.id}` : 'Add a wakeup'}</div><button className="icon-btn" onClick={onClose} aria-label="Close">×</button></div>
        {w && <div className="sub" style={{ fontSize: 12.5 }}>{{ planned: 'Planned by the assistant', fallback: 'Fallback, added by control', sweep: 'Sweep, added by control', routine: 'A routine', owner: 'Yours' }[w.kind]} · {w.status}{w.status_reason ? ` · ${w.status_reason}` : ''}</div>}
        <div style={{ display: 'flex', gap: 6 }}>
          <input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} disabled={!editable} aria-label="Date" />
          <input className="input" type="time" value={time} onChange={(e) => setTime(e.target.value)} disabled={!editable} aria-label="Time" />
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <label className="stack" style={{ gap: 3, flex: 1 }}><span className="sub" style={{ fontSize: 12 }}>Budget (pts)</span><input className="input" type="number" min={1} value={budget} onChange={(e) => setBudget(Number(e.target.value))} disabled={!editable} /></label>
          <label className="stack" style={{ gap: 3, flex: 1 }}><span className="sub" style={{ fontSize: 12 }}>Tier</span>
            <select className="input" value={tier} onChange={(e) => setTier(e.target.value)} disabled={!editable}>{Array.from(new Set([tier, ...tiers])).map((t) => <option key={t}>{t}</option>)}</select></label>
        </div>
        <input className="input" placeholder="Reason" value={reason} onChange={(e) => setReason(e.target.value)} disabled={!editable} />
        {w?.run_id && <Link to={`/runs/${w.run_id}`} className="sub">Run #{w.run_id}{w.used != null ? ` · used ${w.used} pts` : ''}</Link>}
        {err && <div className="error">{err}</div>}
        {editable && (
          <div className="btns" style={{ justifyContent: 'flex-end' }}>
            {w && <button className="btn danger" onClick={() => cancel.mutate(w.id, { onSuccess: onClose, onError: (x) => setErr(problem(x)) })}>Cancel wakeup</button>}
            <button className="btn primary" onClick={save} disabled={create.isPending || patch.isPending}>{w ? 'Save' : 'Add'}</button>
          </div>
        )}
      </div>
    </>
  )
}
