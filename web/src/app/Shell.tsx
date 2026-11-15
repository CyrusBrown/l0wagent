import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { useLive } from '../api/live'
import { useInboxCounts, useProjects, useStatus, useStopRun, useThreads } from '../api/queries'
import { pts, until, usageText } from '../components/format'
import { useOnline } from '../components/online'
import { Palette } from './Palette'
import { useTheme } from './theme'

const CONVERSATION = /^\/(t|p|projects|new)(\/|$)/

export function Shell() {
  const live = useLive(true)
  const online = useOnline()
  const loc = useLocation()
  const nav = useNavigate()
  const [palette, setPalette] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const inConversation = CONVERSATION.test(loc.pathname)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPalette((p) => !p)
      }
      const typing = /INPUT|TEXTAREA|SELECT/.test((e.target as HTMLElement)?.tagName ?? '')
      if (!typing && !e.metaKey && !e.ctrlKey && e.key === 'n') nav('/new')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav])

  return (
    <div className="app">
      <Header onSearch={() => setPalette(true)} moreOpen={moreOpen} setMoreOpen={setMoreOpen} />
      {(!online || live === 'offline') && (
        <div className="offline-bar">{!online ? 'Offline. Messages wait on this device until the connection returns.' : 'Reconnecting to the server…'}</div>
      )}
      <div className="body">
        {inConversation && <Sidebar />}
        <main className="main" style={loc.pathname.startsWith('/t/') ? { display: 'flex', overflow: 'hidden' } : undefined}>
          <Outlet />
        </main>
      </div>
      {palette && <Palette onClose={() => setPalette(false)} />}
    </div>
  )
}

function Header({ onSearch, moreOpen, setMoreOpen }: { onSearch: () => void; moreOpen: boolean; setMoreOpen: (b: boolean) => void }) {
  const counts = useInboxCounts()
  const { theme, toggle } = useTheme()
  const loc = useLocation()
  const convActive = CONVERSATION.test(loc.pathname)
  const more = [['/calendar', 'Calendar', 'Wakeups, routines, protected hours'], ['/network', 'Network', 'Request log and egress rules'], ['/web', 'Web page', "The assistant's web port"]]
  const moreCur = more.find(([to]) => loc.pathname.startsWith(to))
  return (
    <header className="header">
      <Link to="/" className="logo" title="Home">l0wagent</Link>
      <Link to="/new" className="new-btn" title="New chat or thread (N)" style={{ textDecoration: 'none' }}>+</Link>
      <span className="vsep" />
      <nav className="nav">
        <NavLink to="/projects" className={() => (convActive ? 'active' : '')}>Conversations</NavLink>
        <NavLink to="/inbox">Inbox{!!counts.data?.waiting && <span className="count">{counts.data.waiting}</span>}</NavLink>
        <NavLink to="/runs">Runs</NavLink>
        <NavLink to="/files">Files</NavLink>
        <NavLink to="/tools">Tools</NavLink>
        <div style={{ position: 'relative' }}>
          <button className={moreCur ? 'active' : ''} style={moreCur ? { color: 'var(--fg)', background: 'var(--active)' } : undefined} onClick={() => setMoreOpen(!moreOpen)}>
            {moreCur ? moreCur[1] : 'More'} <span style={{ fontSize: 10 }}>▾</span>
          </button>
          {moreOpen && <>
            <div style={{ position: 'fixed', inset: 0, zIndex: 40 }} onClick={() => setMoreOpen(false)} />
            <div style={{ position: 'fixed', zIndex: 41, marginTop: 6, minWidth: 220, background: 'var(--card)', border: '1px solid var(--line)', borderRadius: 12, boxShadow: 'var(--shadow)', padding: 6 }}>
              {more.map(([to, label, desc]) => (
                <Link key={to} to={to} onClick={() => setMoreOpen(false)} style={{ display: 'flex', flexDirection: 'column', padding: '8px 10px', borderRadius: 8, color: 'var(--fg)', textDecoration: 'none' }}>
                  <span style={{ fontWeight: 500, fontSize: 13.5 }}>{label}</span><span className="sub">{desc}</span>
                </Link>
              ))}
            </div>
          </>}
        </div>
      </nav>
      <div className="spacer" />
      <button className="search-btn" onClick={onSearch} title="Search (⌘K)"><span>Search…</span><kbd>⌘K</kbd></button>
      <StatusChip />
      <button className="icon-btn" onClick={toggle} title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>{theme === 'dark' ? '☀' : '☾'}</button>
      <NavLink to="/settings" className={({ isActive }) => `icon-btn ${isActive ? 'active' : ''}`} title="Settings">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="17" x2="20" y2="17" /><circle cx="9" cy="7" r="2.4" fill="var(--side)" /><circle cx="15" cy="17" r="2.4" fill="var(--side)" /></svg>
      </NavLink>
    </header>
  )
}

function StatusChip() {
  const { data: st } = useStatus()
  const stop = useStopRun()
  if (!st) return <span className="chip"><span className="dot" />…</span>
  const working = st.state === 'working'
  const label = { working: 'Working', asleep: 'Asleep', dormant: 'Dormant', paused: 'Paused' }[st.state]
  const text = working ? st.now : st.next_wakeup_at ? `next wakeup ${until(st.next_wakeup_at)}` : 'no wakeups planned'
  const budget = st.budget
    ? working && st.run_budget != null ? `${pts(st.run_used)} / ${pts(st.run_budget)} ${st.budget.unit}` : `${pts(st.budget.left)} ${st.budget.unit} left this week`
    : usageText(st.usage)
  return <>
    <Link to={working && st.run_id ? `/runs/${st.run_id}` : '/runs'} className="chip" title="Open runs">
      <span className={`dot ${working ? 'working' : ''}`} />
      <span style={{ fontWeight: 500, flex: 'none' }}>{label}</span>
      <span className="muted">{text}</span>
      <span style={{ color: 'var(--dim)' }}>·</span>
      <span className="muted">{budget}</span>
    </Link>
    {working && st.run_id && (
      <button className="btn small danger" style={{ borderRadius: 999 }} disabled={stop.isPending} onClick={() => stop.mutate(st.run_id!)}>Stop</button>
    )}
  </>
}

function Sidebar() {
  const projects = useProjects()
  const threads = useThreads()
  const general = (threads.data ?? []).filter((t) => t.project === null)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const loc = useLocation()
  const active = (projects.data ?? []).filter((p) => p.status !== 'archived')
  const currentId = Number(/^\/t\/(\d+)/.exec(loc.pathname)?.[1])
  const currentProject = (threads.data ?? []).find((t) => t.id === currentId)?.project
  return (
    <aside className="side">
      <div className="side-label">General</div>
      {general.map((t) => <ThreadLink key={t.id} t={t} indent={false} />)}
      <div className="side-label">Projects <Link to="/projects" className="sub">All</Link></div>
      {active.map((p) => {
        const isOpen = open[p.slug] ?? (currentProject === p.slug || loc.pathname === `/p/${p.slug}`)
        const pts = (threads.data ?? []).filter((t) => t.project === p.slug)
        return (
          <div key={p.slug}>
            <div style={{ display: 'flex' }}>
              <button className="row" onClick={() => setOpen({ ...open, [p.slug]: !isOpen })} aria-expanded={isOpen}>
                <span className="caret">{isOpen ? '▾' : '▸'}</span>
                <span className="grow">{p.name}</span>
                {p.unread > 0 && <span className="badge">{p.unread}</span>}
              </button>
              <Link to={`/p/${p.slug}`} className="sub" style={{ width: 'auto', padding: '6px 8px' }} title="Open project">›</Link>
            </div>
            {isOpen && pts.map((t) => <ThreadLink key={t.id} t={t} indent />)}
          </div>
        )
      })}
    </aside>
  )
}

function ThreadLink({ t, indent }: { t: { id: number; title: string; unread: number }; indent: boolean }) {
  return (
    <NavLink to={`/t/${t.id}`} className={({ isActive }) => [indent && 'indent', isActive && 'active', t.unread > 0 && 'unread'].filter(Boolean).join(' ')}>
      <span className="grow">{t.title}</span>
      {t.unread > 0 && <span className="badge">{t.unread}</span>}
    </NavLink>
  )
}
