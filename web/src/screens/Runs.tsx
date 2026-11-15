import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import { useJobs, useRun, useRuns, useStopRun, useTimeline } from '../api/queries'
import { ClaimCard } from '../components/Cards'
import { ago, clock, duration, pts, until } from '../components/format'

const statusPill: Record<S['Run']['status'], string> = { starting: 'run', running: 'run', done: 'ok', partial: 'warn', stopped: 'mute', failed: 'bad' }
const whyLabel = (r: S['Run']) =>
  ({ planned: 'Planned wakeup', manual: 'Started by you', fallback: 'Fallback', sweep: 'Sweep', promoted: 'Promoted reply', routine: 'Routine', owner_message: 'Reply now' })[r.why] ?? r.why

export function Runs() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'jobs' ? 'jobs' : 'runs'
  const [kind, setKind] = useState<S['Run']['kind'] | undefined>()
  const runs = useRuns(kind)
  const jobs = useJobs()
  const nav = useNavigate()
  return (
    <div className="page">
      <h1>Runs</h1>
      <div className="tabs">
        <button className={tab === 'runs' ? 'on' : ''} onClick={() => setParams({})}>Runs</button>
        <button className={tab === 'jobs' ? 'on' : ''} onClick={() => setParams({ tab: 'jobs' })}>Long jobs · {jobs.data?.length ?? 0}</button>
      </div>
      {tab === 'runs' ? <>
        <div className="pills">
          {([[undefined, 'All kinds'], ['wakeup', 'Wakeups'], ['reply', 'Replies'], ['routine', 'Routines'], ['reviewer', 'Reviewer']] as const).map(([k, l]) => (
            <button key={l} className={`filter ${kind === k ? 'on' : ''}`} onClick={() => setKind(k)}>{l}</button>
          ))}
        </div>
        {runs.isSuccess && runs.data.length === 0 && <div className="empty">No runs yet. The assistant's first wakeup will show up here.</div>}
        <table className="table">
          <thead><tr><th>#</th><th>About</th><th>Why</th><th>Started</th><th>Budget</th><th>Status</th></tr></thead>
          <tbody>
            {runs.data?.map((r) => (
              <tr key={r.id} className="click" onClick={() => nav(`/runs/${r.id}`)}>
                <td className="sub">#{r.id}</td>
                <td><Link to={`/runs/${r.id}`} style={{ color: 'var(--fg)' }}>{r.about ?? whyLabel(r)}</Link></td>
                <td className="sub">{whyLabel(r)}{r.why_detail ? ` · ${r.why_detail}` : ''}</td>
                <td className="sub">{ago(r.started_at)} · {duration(r.started_at, r.ended_at)}</td>
                <td style={{ minWidth: 110 }}>
                  <div className="sub">{pts(r.used)} / {pts(r.budget)}</div>
                  <div className="gauge"><div style={{ width: `${Math.min(100, (r.used / (r.budget || 1)) * 100)}%` }} /></div>
                </td>
                <td><span className={`pill ${statusPill[r.status]}`}>{r.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </> : <Jobs jobs={jobs.data ?? []} />}
    </div>
  )
}

function Jobs({ jobs }: { jobs: S['Job'][] }) {
  if (!jobs.length) return <div className="empty">No long jobs. Jobs the assistant registers with <code>assistant job start</code> show up here.</div>
  return <>{jobs.map((j) => (
    <div key={j.id} className="card">
      <div className="row-between"><span style={{ fontWeight: 500 }}>{j.label}</span><span className="pill run">{j.progress}</span></div>
      <div className="sub">
        Started {ago(j.started_at)}{j.run_id && <> by <Link to={`/runs/${j.run_id}`}>run #{j.run_id}</Link></>}
        {j.expected_end && ` · expected ${until(j.expected_end)}`}{j.dont_interrupt && " · don't interrupt (holds updates)"}
      </div>
      <pre>{j.log_tail}</pre>
    </div>
  ))}</>
}

export function Run() {
  const id = Number(useParams().id)
  const [view, setView] = useState<'simple' | 'detailed'>('simple')
  const run = useRun(id)
  const timeline = useTimeline(id, view)
  const stop = useStopRun()
  if (run.isError) return <div className="page"><div className="error">Couldn't load run #{id}.</div></div>
  const d = run.data
  if (!d) return <div className="page" />
  const r = d.run
  const meta = [['Why it woke', `${whyLabel(r)}${r.why_detail ? ` · ${r.why_detail}` : ''}`], ['Level', d.level ?? '–'], ['Reviewer', d.reviewer_mode], ['Model', r.tier ?? '–']]
  return (
    <div className="page">
      <div className="page-head">
        <div className="stack" style={{ gap: 4 }}>
          <div className="sub"><Link to="/runs">Runs</Link> / #{r.id}</div>
          <h1>{r.about ?? whyLabel(r)}</h1>
        </div>
        <div className="btns">
          <div className="seg">
            <button className={view === 'simple' ? 'on' : ''} onClick={() => setView('simple')}>Simple</button>
            <button className={view === 'detailed' ? 'on' : ''} onClick={() => setView('detailed')}>Detailed</button>
          </div>
          {r.status === 'running' && <button className="btn danger" disabled={stop.isPending} onClick={() => stop.mutate(r.id)}>Stop</button>}
        </div>
      </div>
      <div className="card" style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 24 }}>
        {meta.map(([k, v]) => <div key={k}><div className="sub">{k}</div><div>{v}</div></div>)}
        <div style={{ flex: 1, minWidth: 180 }}>
          <div className="sub">Budget · {pts(r.used)} of {pts(r.budget)} · {duration(r.started_at, r.ended_at)}{r.deadline && r.status === 'running' ? ` · deadline ${clock(r.deadline)}` : ''}</div>
          <div className="gauge" style={{ marginTop: 8 }}><div style={{ width: `${Math.min(100, (r.used / (r.budget || 1)) * 100)}%` }} /></div>
        </div>
        <span className={`pill ${statusPill[r.status]}`} style={{ alignSelf: 'flex-start' }}>{r.status}</span>
      </div>
      {view === 'simple' ? (
        <div className="grid2">
          <div className="stack" style={{ gap: 22 }}>
            {r.now && <section className="stack"><div className="section-label">Now</div><div className="theirs">{r.now}</div></section>}
            {!!d.claims.length && <section className="stack"><div className="section-label">Active threads</div>{d.claims.map((c) => <ClaimCard key={c.id} c={c} />)}</section>}
            <section className="stack">
              <div className="section-label">Status timeline</div>
              {timeline.data?.map((i) => (
                <div key={i.id} style={{ display: 'flex', gap: 12, fontSize: 13.5 }}>
                  <span className="sub" style={{ width: 44, flex: 'none' }}>{clock(i.at)}</span>
                  <span className={`dot ${i.kind === 'control' || i.kind === 'model' ? 'info' : i.kind === 'ask' ? 'warn' : ''}`} style={{ marginTop: 7 }} />
                  <span style={{ color: i.kind === 'control' ? 'var(--infoInk)' : undefined }}>{i.text}</span>
                </div>
              ))}
            </section>
          </div>
          <section className="card">
            <div className="section-label">Changes</div>
            {([['Memory', d.changes.memory], ['Work files', d.changes.work]] as const).map(([label, files]) => !!files.length && (
              <div key={label} className="stack" style={{ gap: 4 }}>
                <div className="sub">{label}</div>
                {files.map((f) => (
                  <div key={f.path} className="row-between" style={{ fontSize: 13 }}>
                    <code>{f.path}</code>
                    <span><span style={{ color: 'var(--okInk)' }}>+{f.added}</span> <span style={{ color: 'var(--badInk)' }}>−{f.removed}</span></span>
                  </div>
                ))}
              </div>
            ))}
            {([['Shelved', d.changes.shelved], ['Asks filed', d.changes.asks], ['Threads', d.changes.threads]] as const).map(([label, xs]) => !!xs.length && (
              <div key={label} className="stack" style={{ gap: 4 }}><div className="sub">{label}</div>{xs.map((x) => <div key={x} style={{ fontSize: 13 }}>{x}</div>)}</div>
            ))}
          </section>
        </div>
      ) : (
        <div className="stack" style={{ gap: 6 }}>
          {timeline.data?.map((i) => (
            <div key={i.id} id={`t${i.id}`} className="card" style={{ padding: '10px 14px', gap: 6, background: i.kind === 'reviewer' ? 'var(--purpleSoft)' : undefined }}>
              <div className="row-between sub"><span style={{ textTransform: 'capitalize' }}>{i.kind}</span><a href={`#t${i.id}`}>{clock(i.at)}</a></div>
              <div style={{ fontSize: 13.5, fontStyle: i.kind === 'thinking' ? 'italic' : undefined }}>{i.text}</div>
              {typeof i.data.patch === 'string' && <pre>{i.data.patch}</pre>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
