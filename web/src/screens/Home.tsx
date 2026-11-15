import { Link } from 'react-router'
import { useClaims, useInbox, useJobs, useMe, useRecent, useStatus } from '../api/queries'
import { ClaimCard, InboxCard } from '../components/Cards'
import { ago, pts, until } from '../components/format'
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

          {st && (
            <section className="card">
              <div className="section-label">Budget this week</div>
              <div><span style={{ font: '500 28px var(--serif)' }}>{pts(st.budget.left)}</span> <span className="sub">of {pts(st.budget.allowance)} {st.budget.unit} left</span></div>
              <div className="gauge" style={{ display: 'flex' }}>
                <div style={{ width: `${(st.budget.used_by_owner / st.budget.allowance) * 100}%`, background: 'var(--okInk)', borderRadius: 0 }} />
                <div style={{ width: `${(st.budget.used_by_assistant / st.budget.allowance) * 100}%`, borderRadius: 0 }} />
              </div>
              <div className="row-between sub">
                <span><span className="dot ok" style={{ display: 'inline-block', marginRight: 6 }} />You · {pts(st.budget.used_by_owner)}</span>
                <span><span className="dot working" style={{ display: 'inline-block', marginRight: 6, animation: 'none' }} />Assistant · {pts(st.budget.used_by_assistant)}</span>
              </div>
            </section>
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
