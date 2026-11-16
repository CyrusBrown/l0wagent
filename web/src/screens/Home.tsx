import { Link } from 'react-router'
import type { S } from '../api/client'
import { useClaims, useInbox, useJobs, useMe, useRecent, useRun, useStatus, useWebPage } from '../api/queries'
import { ClaimCard, InboxCard } from '../components/Cards'
import { ago, clock, duration, pts, until } from '../components/format'
import { Markdown } from '../components/Markdown'

function greeting(d = new Date()) {
  const h = d.getHours()
  return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

export function Home() {
  const me = useMe()
  const status = useStatus()
  const waiting = useInbox('waiting')
  const recent = useRecent('assistant')
  const claims = useClaims()
  const jobs = useJobs()
  const web = useWebPage()
  const st = status.data
  const top = (waiting.data ?? []).slice(0, 3)

  return (
    <div className="page">
      <div className="stack" style={{ gap: 4 }}>
        <h1>{greeting()}{me.data?.display_name ? `, ${me.data.display_name}` : ''}</h1>
        {st && (
          <div className="sub" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13.5 }}>
            <span className={`dot ${st.state === 'working' ? 'working' : ''}`} />
            {st.state === 'working' ? <>Working · {st.now}</> : st.next_wakeup_at ? <>Asleep · next wakeup {until(st.next_wakeup_at)}</> : <>Dormant · no wakeups planned</>}
          </div>
        )}
      </div>

      <div className="grid2">
        <div className="stack" style={{ gap: 22 }}>
          <section className="stack">
            <div className="section-label">Waiting on you <Link to="/inbox">Open Inbox · {waiting.data?.length ?? 0}</Link></div>
            {waiting.isSuccess && top.length === 0 && <div className="empty">Nothing waiting on you.</div>}
            {top.map((e) => (
              <div key={e.kind + e.id} className="stack" style={{ gap: 4 }}>
                {e.thread_id && <Link className="sub" to={`/t/${e.thread_id}`}>#{e.thread_title}</Link>}
                <InboxCard e={e} />
              </div>
            ))}
          </section>

          <section className="stack">
            <div className="section-label">Recent from the assistant</div>
            {recent.isSuccess && recent.data.length === 0 && <div className="empty">No messages yet. The assistant writes here after its first run.</div>}
            {recent.data?.map((r) => (
              <Link key={r.message.id} to={`/t/${r.message.thread_id}`} className="card" style={{ color: 'var(--fg)', textDecoration: 'none', gap: 6 }}>
                <div className="row-between sub"><span>#{r.thread_title}</span><span>{ago(r.message.created_at)}</span></div>
                <div className="theirs" style={{ fontSize: 14.5, display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                  <Markdown>{r.message.body}</Markdown>
                </div>
              </Link>
            ))}
          </section>
        </div>

        <div className="stack" style={{ gap: 22 }}>
          {!!claims.data?.length && (
            <section className="stack">
              <div className="section-label">Working on</div>
              {claims.data.map((c) => (
                <div key={c.id} className="stack" style={{ gap: 4 }}>
                  <Link className="sub" to={`/t/${c.thread_id}`}>#{c.thread_title}</Link>
                  <ClaimCard c={c} />
                </div>
              ))}
            </section>
          )}

          {st?.state === 'working' && st.run_id && <CurrentRun id={st.run_id} />}

          {st?.budget ? <BudgetCard b={st.budget} /> : st && <UsageCard u={st.usage} />}

          {web.data?.reachable && (
            <Link to="/web" className="card" style={{ color: 'var(--fg)', textDecoration: 'none', gap: 4 }}>
              <div className="section-label"><span>The assistant's web page</span><span className="dot ok" /></div>
              <div style={{ fontSize: 13.5 }}>Something is running on its port 8787. Open it</div>
            </Link>
          )}

          <section className="stack">
            <div className="section-label">Long jobs <Link to="/runs?tab=jobs">All jobs</Link></div>
            {jobs.isSuccess && jobs.data.length === 0 && <div className="empty">No long jobs running.</div>}
            {jobs.data?.map((j) => (
              <div key={j.id} className="card" style={{ gap: 4 }}>
                <div className="row-between"><span style={{ fontWeight: 500 }}>{j.label}</span><span className="sub">{j.progress}</span></div>
                <div className="sub">{j.dont_interrupt ? "Don't interrupt · " : ''}{j.expected_end ? `ends ${until(j.expected_end)}` : ''}</div>
              </div>
            ))}
          </section>
        </div>
      </div>
    </div>
  )
}

function CurrentRun({ id }: { id: number }) {
  const run = useRun(id)
  const r = run.data?.run
  if (!r) return null
  return (
    <Link to={`/runs/${id}`} className="card" style={{ color: 'var(--fg)', textDecoration: 'none' }}>
      <div className="section-label"><span>Current run · #{id}</span><span>{duration(r.started_at)}{r.deadline ? ` · until ${clock(r.deadline)}` : ''}</span></div>
      {r.about && <div style={{ fontSize: 14 }}>{r.about}</div>}
      <div className="sub" style={{ display: 'flex', alignItems: 'center', gap: 8 }}><span className="dot working" />{r.now ?? 'Starting…'}</div>
    </Link>
  )
}

function BudgetCard({ b }: { b: S['BudgetSummary'] }) {
  return (
    <section className="card">
      <div className="section-label"><span>Budget this week</span><Link to="/calendar">Calendar</Link></div>
      <div><span style={{ font: '500 28px var(--serif)' }}>{pts(b.left)}</span> <span className="sub">of {pts(b.allowance)} {b.unit} left</span></div>
      <div className="gauge"><div style={{ width: `${b.allowance ? Math.min(100, ((b.allowance - b.left) / b.allowance) * 100) : 100}%` }} /></div>
      <div className="row-between sub">
        <span><span className="dot ok" style={{ display: 'inline-block', marginRight: 6 }} />You · {pts(b.used_by_owner)}</span>
        <span><span className="dot info" style={{ display: 'inline-block', marginRight: 6, background: 'var(--infoInk)' }} />Your chats · {pts(b.used_by_chat)}</span>
        <span><span className="dot working" style={{ display: 'inline-block', marginRight: 6, animation: 'none' }} />Assistant · {pts(b.used_by_assistant)}</span>
      </div>
    </section>
  )
}

/** Until budgets exist (M4): the provider's own meter. */
function UsageCard({ u }: { u: S['UsageReading'] | null | undefined }) {
  const known = u?.seven_day_pct != null
  return (
    <section className="card">
      <div className="section-label"><span>Claude usage this week</span>{u && <span>as of {clock(u.fetched_at)}</span>}</div>
      {known ? <>
        <div><span style={{ font: '500 28px var(--serif)' }}>{Math.round(u!.seven_day_pct!)}%</span> <span className="sub">used{u!.seven_day_resets_at ? ` · resets ${until(u!.seven_day_resets_at)}` : ''}</span></div>
        <div className="gauge"><div style={{ width: `${Math.min(100, u!.seven_day_pct!)}%` }} /></div>
        {u!.five_hour_pct != null && <div className="sub">5-hour window {Math.round(u!.five_hour_pct)}%{u!.five_hour_resets_at ? ` · resets ${until(u!.five_hour_resets_at)}` : ''}</div>}
      </> : <div className="sub">Unknown{u?.error ? `: ${u.error}` : ''}. <Link to="/settings/system">System</Link></div>}
    </section>
  )
}
