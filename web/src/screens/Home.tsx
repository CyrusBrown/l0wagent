import { Link, useNavigate } from 'react-router'
import type { S } from '../api/client'
import { useCalendar, useClaims, useInbox, useJobs, useMe, useRecent, useStatus, useSystem, useUpdateInfo, useWebPage } from '../api/queries'
import { useTimeFormat } from '../app/timeFormat'
import { CardPlace, InboxCard } from '../components/Cards'
import { ago, pts, stamp } from '../components/format'
import { Markdown } from '../components/Markdown'

// Home, ported from the mockup's HOME screen: the greeting and the assistant's state; on the
// left what's waiting on you and recent messages; on the right what it's working on, what's
// coming up, the budget, long jobs, its web page and the system line.

function greeting(d = new Date()) {
  const h = d.getHours()
  return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

const STATE: Record<string, string> = { working: 'Working', asleep: 'Asleep', dormant: 'Dormant', paused: 'Paused' }
const CLAIM: Record<string, [string, string]> = { in_progress: ['In progress', 'var(--accentInk)'], planned: ['Planned', 'var(--infoInk)'], done: ['Done', 'var(--okInk)'], dropped: ['Dropped', 'var(--muted)'] }

export function Home() {
  useTimeFormat()
  const me = useMe()
  const status = useStatus()
  const waiting = useInbox('waiting')
  const recent = useRecent('assistant')
  const claims = useClaims()
  const jobs = useJobs()
  const web = useWebPage()
  const cal = useCalendar()
  const nav = useNavigate()
  const st = status.data
  const top = [...(waiting.data ?? [])].sort((a, b) => Number(b.kind === 'security') - Number(a.kind === 'security')).slice(0, 3)  // security first
  const upcoming = (cal.data?.wakeups ?? []).filter((w) => w.status === 'planned').slice(0, 3)
  const stateText = !st ? '' : st.state === 'working' ? (st.now ?? 'Starting…') : st.next_wakeup_at ? `next wakeup ${stamp(st.next_wakeup_at)}` : 'no wakeups planned'

  return (
    <div className="home">
      <div className="home-head">
        <h1>{greeting()}{me.data?.display_name ? `, ${me.data.display_name}` : ''}</h1>
        {st && <div className="home-state"><span className={`dot ${st.state === 'working' ? 'working' : ''}`} /><span className="s">{STATE[st.state]}</span><span>{stateText}</span></div>}
      </div>

      <div className="home-grid">
        <div className="home-col" style={{ gap: 28 }}>
          <div className="home-sec" style={{ gap: 10 }}>
            <div className="home-label"><span>Waiting on you</span><Link to="/inbox">Open Inbox · {waiting.data?.length ?? 0}</Link></div>
            {waiting.isSuccess && top.length === 0 && <div className="sub">Nothing waiting on you.</div>}
            {top.map((e) => (
              <CardPlace.Provider key={e.kind + e.id} value={{ mode: 'home', where: e.thread_id ? <Link to={`/t/${e.thread_id}`}>#{e.thread_title}</Link> : undefined }}>
                <InboxCard e={e} />
              </CardPlace.Provider>
            ))}
          </div>
          <div className="home-sec" style={{ gap: 4 }}>
            <div className="home-label"><span>Recent from the assistant</span></div>
            {recent.isSuccess && recent.data.length === 0 && <div className="sub">No messages yet. The assistant writes here after its first run.</div>}
            {recent.data?.map((r) => (
              <div key={r.message.id} className="recent-row" role="link" tabIndex={0} onClick={() => nav(`/t/${r.message.thread_id}`)} onKeyDown={(e) => { if (e.key === 'Enter') nav(`/t/${r.message.thread_id}`) }}>
                <div className="row-between meta"><span className="t">#{r.thread_title}</span><span>{ago(r.message.created_at)}</span></div>
                <div className="body"><Markdown>{r.message.body}</Markdown></div>
              </div>
            ))}
          </div>
        </div>

        <div className="home-col" style={{ gap: 24 }}>
          {!!claims.data?.length && (
            <div className="home-sec" style={{ gap: 10 }}>
              <div className="home-label"><span>Working on</span></div>
              {claims.data.map((c) => (
                <Link key={c.id} to={`/t/${c.thread_id}`} className="work-card">
                  <div className="row-between meta"><span>#{c.thread_title}</span><span style={{ color: CLAIM[c.status]?.[1] }}>{CLAIM[c.status]?.[0] ?? c.status}</span></div>
                  <span className="title">{c.plan}</span>
                </Link>
              ))}
            </div>
          )}
          <div className="home-sec" style={{ gap: 6 }}>
            <div className="home-label"><span>Coming up</span><Link to="/calendar">Calendar</Link></div>
            {cal.isSuccess && !upcoming.length && <div className="sub" style={{ padding: '8px 0' }}>Nothing planned.</div>}
            {upcoming.map((w) => (
              <div key={w.id} className="up-row"><span className="w">{stamp(w.at)}</span><span className="r">{w.reason}</span><span className="b">{pts(w.budget)} pts</span></div>
            ))}
          </div>
          {st?.budget && <Budget b={st.budget} />}
          {!!jobs.data?.length && (
            <div className="home-sec" style={{ gap: 8 }}>
              <div className="home-label"><span>Long jobs</span><Link to="/runs?tab=jobs">All jobs</Link></div>
              {jobs.data.map((j) => (
                <div key={j.id} className="job-row"><span className="l"><span className="d" /><span className="ellipsis">{j.label}</span></span><span className="mono">{j.progress}</span></div>
              ))}
            </div>
          )}
          {web.data?.reachable && (
            <Link to="/web" className="web-card">
              <div className="stack" style={{ gap: 2 }}><span className="t">Assistant's web page</span><span className="mono">port 8787</span></div>
              <span style={{ color: 'var(--faint)' }}>›</span>
            </Link>
          )}
          <SystemLine />
        </div>
      </div>
    </div>
  )
}

function Budget({ b }: { b: S['BudgetSummary'] }) {
  const whole = Math.max(b.allowance, 1)
  const you = b.used_by_owner + b.used_by_chat
  return (
    <div className="home-sec" style={{ gap: 8 }}>
      <div className="home-label"><span>Budget this {b.period ?? 'week'}</span></div>
      <div className="row-start" style={{ alignItems: 'baseline', gap: 6 }}><span className="big-num">{pts(b.left)}</span><span className="sub" style={{ fontSize: 13 }}>of {pts(b.allowance)} {b.unit} left</span></div>
      <div className="split-bar2">
        <div style={{ width: `${Math.min(100, (you / whole) * 100)}%`, background: 'var(--infoDot)' }} />
        <div style={{ width: `${Math.min(100, (b.used_by_assistant / whole) * 100)}%`, background: 'var(--accent)' }} />
      </div>
      <div className="row-start legend"><span><i style={{ background: 'var(--infoDot)' }} />You · {pts(you)}</span><span><i style={{ background: 'var(--accent)' }} />Assistant · {pts(b.used_by_assistant)}</span></div>
    </div>
  )
}

function SystemLine() {
  const sys = useSystem().data
  const up = useUpdateInfo().data
  if (!sys) return null
  const parts = [
    sys.runner.reachable ? "the assistant's computer is up" : "the assistant's computer isn't reachable",
    sys.gate.open ? 'runs can start' : `runs held: ${sys.gate.reason ?? 'gate closed'}`,
    up?.available ? `l0wagent ${up.latest} available` : `l0wagent ${sys.server_version}`,
  ]
  return (
    <div className="home-sec" style={{ gap: 6 }}>
      <div className="home-label"><span>System</span></div>
      <div className="sys-line">{parts.join(' · ')}</div>
    </div>
  )
}
