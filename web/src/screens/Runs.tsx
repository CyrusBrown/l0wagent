import { type CSSProperties, useState } from 'react'
import { ExtSlot } from '../ext/ExtElement'
import { slotItems } from '../ext/registry'
import { useExtensions } from '../ext/useExtensions'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import { problem, useAddRunBudget, useJobs, useProjects, useRun, useRuns, useStartRun, useStopRun, useTimeline } from '../api/queries'
import { useTimeFormat } from '../app/timeFormat'
import { ago, clock, clockSeconds, duration, pts, stamp, until } from '../components/format'
import { Select } from '../components/Select'
import { CodeBox } from '../components/CopyCode'

// Runs and one run, ported from the mockup's RUNS LIST and RUN screens. A run opens as its
// summary; the status timeline card opens the full (detailed) timeline, with "← Back to summary".
// Additions not in the mockup: "Wake now" (REVIEW.md #24), Reply runs in the filters.

const STATUS: Record<S['Run']['status'], [string, string]> = {
  starting: ['Starting', 'run'], running: ['Running', 'run'], done: ['Done', 'ok'], partial: ['Partial', 'warn'], stopped: ['Stopped', 'mute'], failed: ['Failed', 'bad'],
}
const CLAIM: Record<string, string> = { in_progress: 'run', planned: 'info', done: 'ok', dropped: 'mute' }
const whyLabel = (r: S['Run']) =>
  ({ planned: 'Planned wakeup', manual: 'Started by you', fallback: 'Fallback', sweep: 'Sweep', promoted: 'Promoted reply', routine: 'Routine', owner_message: 'Reply now', owner: 'Your wakeup' })[r.why] ?? r.why
const FILTERS: [string, (r: S['Run']) => boolean][] = [
  ['All kinds', () => true], ['Planned', (r) => r.why === 'planned' || r.why === 'owner' || r.why === 'manual'], ['Routine', (r) => r.why === 'routine'],
  ['Sweep', (r) => r.why === 'sweep'], ['Promoted', (r) => r.why === 'promoted'], ['Replies', (r) => r.kind === 'reply'],
]
const cap = (x: string) => x[0].toUpperCase() + x.slice(1)

export function Runs() {
  useTimeFormat()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'jobs' ? 'jobs' : 'runs'
  const [filter, setFilter] = useState(0)
  const [project, setProject] = useState('')
  const runs = useRuns(undefined, project || undefined)
  const jobs = useJobs()
  const projects = useProjects()
  const nav = useNavigate()
  const start = useStartRun()
  const shown = (runs.data ?? []).filter(FILTERS[filter][1])
  return (
    <div className="page runs-page">
      <div className="page-head">
        <h1 className="runs-title">Runs</h1>
        <button className="btn primary" disabled={start.isPending} title="Start a run now (manual wakeup)"
          onClick={() => start.mutate({ reason: 'manual' }, { onSuccess: (r) => nav(`/runs/${r.id}`) })}>
          {start.isPending ? 'Waking…' : 'Wake now'}
        </button>
      </div>
      {start.error && <div className="error" role="alert">{problem(start.error)}</div>}
      <div className="tabs">
        <button className={tab === 'runs' ? 'on' : ''} onClick={() => setParams({})}>Runs</button>
        <button className={tab === 'jobs' ? 'on' : ''} onClick={() => setParams({ tab: 'jobs' })}>Long jobs · {jobs.data?.length ?? 0}</button>
      </div>
      {tab === 'runs' ? <>
        <div className="pills">
          {FILTERS.map(([l], i) => <button key={l} className={`filter ${filter === i ? 'on' : ''}`} onClick={() => setFilter(i)}>{l}</button>)}
          <Select variant="filter" className={project ? 'on' : ''} value={project} label="Project"
            options={[{ value: '', label: 'All projects' }, ...(projects.data?.map((p) => ({ value: p.slug, label: p.name })) ?? [])]}
            onChange={setProject} />
        </div>
        {runs.isSuccess && shown.length === 0
          ? <div className="empty">{runs.data.length ? 'No runs match.' : "No runs yet. The assistant's first wakeup will show up here."}</div>
          : (
            <div className="run-list">
              {shown.map((r) => (
                <div key={r.id} className="run-row" role="link" tabIndex={0} onClick={() => nav(`/runs/${r.id}`)} onKeyDown={(e) => { if (e.key === 'Enter') nav(`/runs/${r.id}`) }}>
                  <span className="run-id">#{r.id}</span>
                  <div className="run-about">
                    <span className="ellipsis">{r.about ?? whyLabel(r)}</span>
                    <span className="sub">{whyLabel(r)}{r.why_detail ? ` · ${r.why_detail}` : ''}</span>
                  </div>
                  <div className="run-when"><span>{stamp(r.started_at)}</span><span>{duration(r.started_at, r.ended_at)}{r.status === 'running' ? ' so far' : ''}</span></div>
                  <div className="run-spend">
                    <span>{pts(r.used)} / {pts(r.budget)} pts</span>
                    <div className="bar"><div style={{ width: `${Math.min(100, (r.used / (r.budget || 1)) * 100)}%` }} /></div>
                  </div>
                  <span className={`pill ${STATUS[r.status][1]}`} style={{ justifySelf: 'end' }}>{STATUS[r.status][0]}</span>
                </div>
              ))}
            </div>
          )}
      </> : <Jobs jobs={jobs.data ?? []} />}
    </div>
  )
}

function Jobs({ jobs }: { jobs: S['Job'][] }) {
  if (!jobs.length) {
    return (
      <div className="empty-big">
        <div className="empty-title">No long jobs running</div>
        <div className="sub">When the assistant starts something that outlives a run, like a backup or a long index, it shows up here with its log.</div>
      </div>
    )
  }
  return <>{jobs.map((j) => (
    <div key={j.id} className="job-card">
      <div className="job-head">
        <span className="dot working" />
        <div className="stack" style={{ gap: 3, flex: 1, minWidth: 200 }}>
          <div className="row-start" style={{ flexWrap: 'wrap' }}>
            <span style={{ font: '500 14.5px var(--sans)' }}>{j.label}</span>
            {j.dont_interrupt && <span className="pill warn">Don't interrupt</span>}
          </div>
          <span className="sub">
            Started {j.run_id ? <>by <Link to={`/runs/${j.run_id}`}>run #{j.run_id}</Link> · </> : ''}{ago(j.started_at)}{j.expected_end && (new Date(j.expected_end).getTime() > Date.now()
              ? ` · expected ${until(j.expected_end)}`
              : <> · <span style={{ color: 'var(--warnInk)' }}>running past its estimate ({stamp(j.expected_end)})</span></>)}
          </span>
        </div>
        <span className="mono" style={{ fontSize: 13, color: 'var(--fg2)' }}>{j.progress}</span>
      </div>
      <CodeBox text={j.log_tail}><pre className="job-log">{j.log_tail}</pre></CodeBox>
      {j.dont_interrupt && <div className="job-foot">Updates are held until this job ends.</div>}
    </div>
  ))}</>
}

export function Run() {
  useTimeFormat()
  const id = Number(useParams().id)
  const [params, setParams] = useSearchParams()
  const detailed = params.get('view') === 'detailed'
  const run = useRun(id)
  const stop = useStopRun()
  const more = useAddRunBudget()
  if (run.isError) return <div className="page"><div className="error">Couldn't load run #{id}.</div></div>
  const d = run.data
  if (!d) return <div className="page" />
  const r = d.run
  const live = r.status === 'running' || r.status === 'starting'
  const meta = [
    ['Why it woke', `${whyLabel(r)}${r.why_detail ? ` · ${r.why_detail}` : ''}`], ['Level', d.level ? cap(d.level) : '–'], ['Reviewer', cap(d.reviewer_mode)],
    ['Model', r.tier ? r.tier.split('/').map(cap).join(' · ') : '–'], ['Spends', r.bucket === 'reply' ? 'Reply budget' : r.bucket === 'sweep' ? 'Sweep' : 'Allowance'],
  ]
  const t0 = r.started_at ? Date.parse(r.started_at) : NaN
  const elapsed = Date.parse(r.ended_at ?? new Date().toISOString()) - t0
  const total = r.deadline ? Date.parse(r.deadline) - t0 : 0
  return (
    <div className="page run-page">
      <div className="run-head">
        <div className="row-between" style={{ flexWrap: 'wrap' }}>
          <div className="row-start" style={{ gap: 10 }}>
            <h1>Run #{r.id}</h1>
            <span className={`pill ${STATUS[r.status][1]} row-start`} style={{ gap: 6 }}>{live && <span className="dot working" style={{ width: 6, height: 6 }} />}{STATUS[r.status][0]}</span>
          </div>
          {live && (
            <div className="row-start" style={{ gap: 8 }}>
              <button className="btn small" title={`Give this run 10 more points (budget ${r.budget ?? 0} → ${(r.budget ?? 0) + 10})`}
                disabled={more.isPending || stop.isPending} onClick={() => more.mutate({ id: r.id, add: 10 })}>+10 pts</button>
              <button className="btn small" style={{ color: 'var(--badInk)' }} disabled={stop.isPending} onClick={() => stop.mutate(r.id)}>Stop</button>
            </div>
          )}
        </div>
        {more.error && <div className="error" role="alert">{problem(more.error)}</div>}
        <div className="run-meta">
          {meta.map(([k, v]) => <div key={k} className="stack" style={{ gap: 3 }}><span className="k">{k}</span><span className="v">{v}</span></div>)}
          <div className="run-budget">
            <div className="row-between"><span>Budget</span><span className="v">{pts(r.used)} of {pts(r.budget)} pts</span></div>
            <div className="bar big">
              <div style={{ width: `${Math.min(100, (r.used / (r.budget || 1)) * 100)}%` }} />
              {live && total > 0 && Number.isFinite(elapsed) && <span className="time-mark" style={{ left: `${Math.min(100, (elapsed / total) * 100)}%` }} title="Time used" />}
            </div>
            <div className="row-between"><span>{duration(r.started_at, r.ended_at)}{live ? ' elapsed' : ''}</span><span>{r.deadline ? `deadline ${clock(r.deadline)}` : ''}</span></div>
          </div>
        </div>
      </div>
      {detailed
        ? <Detailed id={id} back={() => setParams({})} />
        : <Summary d={d} open={() => setParams({ view: 'detailed' })} />}
      {!detailed && <RunWidgets runId={id} />}
    </div>
  )
}

function RunWidgets({ runId }: { runId: number }) {
  const exts = useExtensions()
  return <>{slotItems(exts.data, 'run_widgets').map(({ ext, item }) => (
    <div key={`${ext.name}:${item.element}`} className="stack" style={{ gap: 8 }}>
      <div className="mini-label">{item.title ?? ext.title}</div>
      <ExtSlot ext={ext} element={item.element} context={{ run_id: runId }} />
    </div>
  ))}</>
}

function Summary({ d, open }: { d: S['RunDetail']; open: () => void }) {
  const r = d.run
  const timeline = useTimeline(r.id, 'simple')
  const live = r.status === 'running' || r.status === 'starting'
  const c = d.changes
  const groups: [string, string][] = [
    ['Memory', c.memory], ['Work files', c.work],
  ].filter(([, f]) => (f as S['FileChange'][]).length).map(([g, f]) => {
    const files = f as S['FileChange'][]
    const a = files.reduce((n, x) => n + (x.added ?? 0), 0), rm = files.reduce((n, x) => n + (x.removed ?? 0), 0)
    return [g as string, `${files.length} ${files.length === 1 ? 'file' : 'files'}${a || rm ? `  +${a} −${rm}` : ''}`]
  })
  for (const [g, xs] of [['Shelved', c.shelved], ['Asks filed', c.asks], ['Threads', c.threads]] as const) {
    if (xs.length) groups.push([g, `${xs.length} ${xs.length === 1 ? 'item' : 'items'}`])
  }
  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="run-now">
        <div className="row-start label">{live && <span className="dot working" />}{live ? 'Now' : STATUS[r.status][0]}</div>
        <div className="headline">{live ? (r.now ?? 'Starting…') : (r.about ?? whyLabel(r))}</div>
        {live && r.about && <div className="para">{r.about}</div>}
      </div>
      <div className="run-grid">
        <div className="tl-card" role="button" tabIndex={0} onClick={open} onKeyDown={(e) => { if (e.key === 'Enter') open() }}>
          <div className="tl-card-head"><span>Status timeline</span><span className="accent">Open full timeline →</span></div>
          <div className="tl-list">
            {timeline.data?.map((i) => {
              const tag = i.kind === 'ask' ? 'ask' : i.kind === 'control' || i.kind === 'model' || i.kind === 'reviewer' || i.kind === 'shelf' ? 'control' : i.kind === 'error' ? 'error' : 'note'
              return (
                <div key={i.id} className={`tl-row tl-${tag}`}>
                  <span className="t">{clock(i.at)}</span><span className="d" /><span className="x">{i.text}</span><span className="tag">{tag}</span>
                </div>
              )
            })}
            {live && r.now && <div className="tl-row now"><span className="t">now</span><span className="d" /><span className="x">{r.now}…</span></div>}
            {timeline.isSuccess && !timeline.data.length && !live && <div className="sub" style={{ padding: '10px 0' }}>Nothing recorded.</div>}
          </div>
        </div>
        <div className="stack" style={{ gap: 20, minWidth: 0 }}>
          <div className="stack" style={{ gap: 8 }}>
            <div className="mini-label">Active threads</div>
            {d.claims.map((cl) => (
              <Link key={cl.id} to={`/t/${cl.thread_id}`} className="claim-mini">
                <div className="row-between"><span>#{cl.thread_title}</span><span className={`ink-${CLAIM[cl.status] ?? 'mute'}`}>{cap(cl.status.replace('_', ' '))}</span></div>
                <span className="title">{cl.plan}</span>
              </Link>
            ))}
            {/* Threads it posted in without a claim: still where it's working. */}
            {(d.threads ?? []).filter((th) => !d.claims.some((cl) => cl.thread_id === th.thread_id)).map((th) => (
              <Link key={th.thread_id} to={`/t/${th.thread_id}`} className="claim-mini">
                <div className="row-between"><span>#{th.title}</span><span className="ink-mute">{th.messages === 1 ? 'Replied' : `${th.messages} messages`}</span></div>
                <span className="sub">Last {clock(th.last_at)} · no claim</span>
              </Link>
            ))}
            {!d.claims.length && !d.threads?.length && <span className="sub">None.</span>}
          </div>
          <div className="stack" style={{ gap: 6 }}>
            <div className="mini-label">Changes</div>
            {groups.length ? groups.map(([g, v]) => <div key={g} className="change-row"><span>{g}</span><span className="mono">{v}</span></div>)
              : <span className="sub">{live ? 'Memory changes show when the run ends.' : 'No changes.'}</span>}
          </div>
        </div>
      </div>
    </div>
  )
}

const DETAIL_FILTERS: [string, (i: S['TimelineItem']) => boolean][] = [
  ['All', () => true], ['Tools', (i) => i.kind === 'tool'], ['Diffs', (i) => i.kind === 'diff'], ['Reviewer', (i) => i.kind === 'reviewer'],
  ['Control', (i) => i.kind === 'control' || i.kind === 'model' || i.kind === 'usage' || i.kind === 'error'],
]
const KIND_INK: Record<string, string> = {
  control: 'var(--infoInk)', model: 'var(--infoInk)', thinking: 'var(--faint)', reviewer: 'var(--purpleInk)', ask: 'var(--warnInk)', diff: 'var(--okInk)', usage: 'var(--faint)', error: 'var(--badInk)',
}
const KIND_LABEL: Record<string, string> = { text: 'Message', tool: 'Tool', control: 'Control' }

/** A command, a tool's output or a patch: wrapped (no sideways scrolling), and long ones fold to `lines` with "Show all". */
function Output({ text, className, lines }: { text: string; className?: string; lines: number }) {
  const [open, setOpen] = useState(false)
  const rows = text.split('\n').length
  const long = rows > lines || text.length > lines * 110
  return (
    <div className="out">
      <CodeBox text={className === 'cmd' ? text.replace(/^\$ /, '') : text}>
        <pre className={`${className ?? ''} ${long && !open ? 'folded' : ''}`} style={{ '--lines': lines } as CSSProperties}>{text}</pre>
      </CodeBox>
      {long && <button className="out-more" onClick={() => setOpen(!open)}>{open ? 'Show less' : `Show all${rows > lines ? ` (${rows} lines)` : ''}`}</button>}
    </div>
  )
}

function Detailed({ id, back }: { id: number; back: () => void }) {
  const timeline = useTimeline(id, 'detailed')
  const [q, setQ] = useState('')
  const [f, setF] = useState(0)
  const [thinking, setThinking] = useState(false)
  const items = (timeline.data ?? []).filter((i) => (thinking || i.kind !== 'thinking') && DETAIL_FILTERS[f][1](i) && (!q.trim() || i.text.toLowerCase().includes(q.trim().toLowerCase())))
  return <>
    <button className="back-link" onClick={back}>← Back to summary</button>
    <div className="stack" style={{ gap: 14 }}>
      <div className="row-start" style={{ gap: 10, flexWrap: 'wrap' }}>
        <input className="input tl-search" placeholder="Search timeline" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search timeline" />
        {DETAIL_FILTERS.map(([l], i) => <button key={l} className={`filter ${f === i ? 'on' : ''}`} onClick={() => setF(i)}>{l}</button>)}
        <button className="switch-label" style={{ marginLeft: 'auto' }} role="switch" aria-checked={thinking} onClick={() => setThinking(!thinking)}>
          <span className={`switch ${thinking ? 'on' : ''}`}><span /></span>Show thinking
        </button>
      </div>
      <div className="detail-list">
        {items.map((i) => {
          const code = typeof i.data.patch === 'string' ? i.data.patch : i.kind === 'tool' && typeof i.data.result === 'string' && i.data.result ? i.data.result : null
          // The step's text is cut at 300 characters; the full command is in the tool's input.
          const input = i.kind === 'tool' ? (i.data.input as Record<string, unknown> | undefined) : undefined
          const cmd = typeof input?.command === 'string' && input.command.length > i.text.length - 2 ? `$ ${input.command}` : null
          return (
            <div key={i.id} id={`t${i.id}`} className={`detail-row ${i.kind === 'reviewer' ? 'reviewer' : ''}`}>
              <span className="t">{clockSeconds(i.at)}</span>
              <span className="k" style={{ color: KIND_INK[i.kind] ?? 'var(--muted)' }}>{KIND_LABEL[i.kind] ?? cap(i.kind)}</span>
              <div className="stack" style={{ gap: 6, minWidth: 0 }}>
                {cmd
                  ? <Output text={cmd} className="cmd" lines={6} />
                  : <span className="x" style={{ fontStyle: i.kind === 'thinking' ? 'italic' : undefined, fontFamily: i.kind === 'tool' ? 'var(--mono)' : undefined, fontSize: i.kind === 'tool' ? 12.5 : undefined }}>{i.text}</span>}
                {i.kind === 'diff' && Array.isArray(i.data.files) && (i.data.files as { path: string; added: number | null; removed: number | null }[]).map((fl) => (
                  <span key={fl.path} className="mono sub">{fl.path}  <span style={{ color: 'var(--okInk)' }}>+{fl.added ?? '?'}</span> <span style={{ color: 'var(--badInk)' }}>−{fl.removed ?? '?'}</span></span>
                ))}
                {code && <Output text={code} className={i.data.is_error ? 'err' : ''} lines={14} />}
              </div>
              <a href={`#t${i.id}`} className="go" title="Link to this step">↗</a>
            </div>
          )
        })}
        {timeline.isSuccess && !items.length && <div className="sub" style={{ padding: 14 }}>Nothing matches.</div>}
      </div>
    </div>
  </>
}
