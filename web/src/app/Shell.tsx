import { type CSSProperties, useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import type { S } from '../api/client'
import { useLive } from '../api/live'
import { useChats, useInboxCounts, useProjects, useResume, useStatus, useStopRun, useThreads, useUpdateInfo, useCancelUpdate } from '../api/queries'
import { pts, stamp, until, usageText } from '../components/format'
import { useOnline } from '../components/online'
import { useEscape } from '../components/useEscape'
import { useOutbox } from '../api/offline'
import { OutboxFailures } from '../components/OutboxFailures'
import { useTimeFormat } from './timeFormat'
import { Palette } from './Palette'
import { Caret } from '../components/Caret'
import { Overlay } from '../components/Overlay'
import { useIsPhone } from '../components/useIsPhone'
import { Icon } from './icons'
import { ExtSlot } from '../ext/ExtElement'
import { pages, slotItems } from '../ext/registry'
import { useExtensions } from '../ext/useExtensions'

const CONVERSATION = /^\/(t|c|p|projects|general|new)(\/|$)/

export function Shell() {
  const live = useLive(true)
  const online = useOnline()
  const waiting = useOutbox().filter((i) => !i.error).length
  const loc = useLocation()
  const nav = useNavigate()
  const [palette, setPalette] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const [sheet, setSheet] = useState(false)
  const phone = useIsPhone()
  const inConversation = CONVERSATION.test(loc.pathname)

  // Phone sheets close on navigation (and when the window grows to a desktop width).
  useEffect(() => { setDrawer(false); setSheet(false) }, [loc.pathname, loc.search])
  useEffect(() => { if (!phone) { setDrawer(false); setSheet(false) } }, [phone])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPalette((p) => !p)
      }
      const el = e.target as HTMLElement | null
      const typing = /INPUT|TEXTAREA|SELECT/.test(el?.tagName ?? '')
      const inDropdown = !!el?.closest?.('.sel-btn, [aria-haspopup="listbox"]')
      if (!typing && !inDropdown && !e.metaKey && !e.ctrlKey && e.key === 'n') nav('/new')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav])

  return (
    <div className="app">
      <Header onSearch={() => setPalette(true)} moreOpen={moreOpen} setMoreOpen={setMoreOpen} onDrawer={phone && inConversation ? () => setDrawer(true) : undefined} drawerOpen={drawer} />
      <PauseBanner />
      <UpdateBanner />
      {(!online || live === 'offline') && (
        <div className="offline-bar">{!online ? `Offline. ${waiting ? `${waiting} item${waiting === 1 ? '' : 's'} waiting on this device until the connection returns.` : 'Thread messages, answers and finished tasks wait on this device until the connection returns; chat needs a connection.'}` : 'Reconnecting to the server…'}</div>
      )}
      <OutboxFailures />
      <div className="body">
        {inConversation && <Sidebar />}
        <main className={/^\/c\//.test(loc.pathname) ? 'main pin' : 'main'} style={/^\/(t\/|files)/.test(loc.pathname) ? { display: 'flex', overflow: 'hidden' } : undefined}>
          <Outlet />
        </main>
      </div>
      {phone && <TabBar chatsActive={inConversation} openDrawer={() => setDrawer(true)} openMore={() => setSheet(true)} moreOpen={sheet} />}
      {phone && inConversation && (
        <Overlay open={drawer} onClose={() => setDrawer(false)} side="left" label="Conversations" className="drawer">
          <div className="drawer-head">
            <span className="drawer-title">Conversations</span>
            <button className="icon-btn" aria-label="Close conversations" onClick={() => setDrawer(false)}>{Icon.close(17)}</button>
          </div>
          <div className="drawer-body" onClick={(e) => { if ((e.target as HTMLElement).closest('a')) setDrawer(false) }}><Sidebar /></div>
        </Overlay>
      )}
      {phone && <MoreSheet open={sheet} onClose={() => setSheet(false)} />}
      {palette && <Palette onClose={() => setPalette(false)} />}
    </div>
  )
}

/** What the More dropdown (desktop) and the More sheet (phone) list; the first three are the dropdown's. */
const MORE_DESKTOP = [['/calendar', 'Calendar', 'Wakeups, routines, protected hours'], ['/network', 'Network', 'Request log and egress rules']]
const MORE_PHONE: [string, string, string, keyof typeof Icon][] = [
  ['/files', 'Files', "The assistant's workspace and project shelves", 'files'],
  ['/tools', 'Tools', 'Integrations, connected apps and secrets', 'tools'],
  ['/calendar', 'Calendar', 'Wakeups, routines, protected hours', 'calendar'],
  ['/network', 'Network', 'Request log and egress rules', 'network'],
  ['/settings', 'Settings', 'Budgets, providers, account, appearance', 'settings'],
]

function Header({ onSearch, moreOpen, setMoreOpen, onDrawer, drawerOpen }: { onSearch: () => void; moreOpen: boolean; setMoreOpen: (b: boolean) => void; onDrawer?: () => void; drawerOpen: boolean }) {
  const counts = useInboxCounts()
  const loc = useLocation()
  const convActive = CONVERSATION.test(loc.pathname)
  const exts = useExtensions()
  const more = [...MORE_DESKTOP, ...pages(exts.data).map((p) => [p.to, p.page.title, p.ext.title])]
  const moreCur = more.find(([to]) => loc.pathname.startsWith(to))
  useEscape(moreOpen, () => setMoreOpen(false))
  return (
    <header className="header">
      {onDrawer && <button className="icon-btn drawer-btn" aria-label="Conversations" aria-haspopup="dialog" aria-expanded={drawerOpen} onClick={onDrawer}>{Icon.menu()}</button>}
      <NavLink to="/" end className={({ isActive }) => `logo ${isActive ? 'active' : ''} ${onDrawer ? 'with-drawer' : ''}`} title="Home">
        <span>l0wagent</span>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3.5 10.5 12 3.5l8.5 7V20a1 1 0 0 1-1 1H15v-6.5H9V21H4.5a1 1 0 0 1-1-1z" /></svg>
      </NavLink>
      <Link to="/new" className="new-btn" title="New chat or thread (N)" style={{ textDecoration: 'none' }}>+</Link>
      <span className="vsep" />
      <nav className="nav">
        <NavLink to="/projects" className={() => (convActive ? 'active' : '')} aria-current={convActive ? 'page' : undefined}>Conversations</NavLink>
        <NavLink to="/inbox" aria-label={counts.data?.waiting ? `Inbox, ${counts.data.waiting} waiting` : undefined}>Inbox{!!counts.data?.waiting && <span className="count" aria-hidden="true">{counts.data.waiting}</span>}</NavLink>
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
      <button className="search-btn" onClick={onSearch} title="Search (⌘K)" aria-label="Search"><span>Search</span><kbd>⌘K</kbd><span className="search-ic">{Icon.search()}</span></button>
      <StatusChip />
      <NavLink to="/settings" className={({ isActive }) => `icon-btn ${isActive ? 'active' : ''}`} title="Settings">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><line x1="4" y1="7" x2="20" y2="7" /><line x1="4" y1="17" x2="20" y2="17" /><circle cx="9" cy="7" r="2.4" fill="var(--side)" /><circle cx="15" cy="17" r="2.4" fill="var(--side)" /></svg>
      </NavLink>
    </header>
  )
}

/** The phone's bottom navigation: five places, thumb height. Chats opens the conversation drawer when
 * you're already in a conversation, and the Projects list otherwise. */
function TabBar({ chatsActive, openDrawer, openMore, moreOpen }: { chatsActive: boolean; openDrawer: () => void; openMore: () => void; moreOpen: boolean }) {
  const counts = useInboxCounts()
  const morePhone = useMorePhone()
  const { pathname } = useLocation()
  const waiting = counts.data?.waiting ?? 0
  const moreActive = morePhone.some(([to]) => pathname.startsWith(to))
  const cur = (on: boolean) => (on ? 'page' : undefined)
  const cls = (on: boolean) => `tab ${on ? 'active' : ''}`
  // One highlight for the whole bar, wrapping icon and label; it slides to the active tab.
  const on = [pathname === '/', chatsActive, pathname.startsWith('/inbox'), pathname.startsWith('/runs'), moreActive || moreOpen]
  const idx = moreOpen ? 4 : on.indexOf(true)
  return (
    <nav className="tabbar" aria-label="Main">
      <span className={`tab-pill ${idx < 0 ? 'none' : ''}`} style={{ '--i': Math.max(idx, 0) } as CSSProperties} aria-hidden="true" />
      <Link to="/" className={cls(pathname === '/')} aria-current={cur(pathname === '/')}><span className="tab-ic">{Icon.home()}</span><span className="tab-label">Home</span></Link>
      <Link to="/projects" className={cls(chatsActive)} aria-current={cur(chatsActive)} onClick={(e) => { if (chatsActive) { e.preventDefault(); openDrawer() } }}>
        <span className="tab-ic">{Icon.chats()}</span><span className="tab-label">Chats</span>
      </Link>
      <Link to="/inbox" className={cls(pathname.startsWith('/inbox'))} aria-current={cur(pathname.startsWith('/inbox'))} aria-label={waiting ? `Inbox, ${waiting} waiting` : undefined}>
        <span className="tab-ic">{Icon.inbox()}{waiting > 0 && <span className="count tab-count" aria-hidden="true">{waiting}</span>}</span><span className="tab-label">Inbox</span>
      </Link>
      <Link to="/runs" className={cls(pathname.startsWith('/runs'))} aria-current={cur(pathname.startsWith('/runs'))}><span className="tab-ic">{Icon.runs()}</span><span className="tab-label">Runs</span></Link>
      <button className={cls(moreActive || moreOpen)} aria-current={cur(moreActive)} aria-haspopup="dialog" aria-expanded={moreOpen} onClick={openMore}>
        <span className="tab-ic">{Icon.more()}</span><span className="tab-label">More</span>
      </button>
    </nav>
  )
}

/** The phone's More list: the built-in places, then any extension pages. */
function useMorePhone(): [string, string, string, keyof typeof Icon][] {
  const exts = useExtensions()
  return [...MORE_PHONE, ...pages(exts.data).map((p): [string, string, string, keyof typeof Icon] => [p.to, p.page.title, p.ext.title, 'more'])]
}

function MoreSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { pathname } = useLocation()
  const morePhone = useMorePhone()
  return (
    <Overlay open={open} onClose={onClose} side="bottom" label="More" className="more-sheet">
      <div className="sheet-title">More</div>
      <div className="sheet-list">
        {morePhone.map(([to, label, desc, icon]) => {
          const on = pathname.startsWith(to)
          return (
            <Link key={to} to={to} className={`sheet-row ${on ? 'on' : ''}`} aria-current={on ? 'page' : undefined} onClick={onClose}>
              <span className="sheet-ic">{Icon[icon]()}</span>
              <span className="sheet-text"><span className="sheet-label">{label}</span><span className="sub">{desc}</span></span>
              <span className="sheet-go" aria-hidden="true">›</span>
            </Link>
          )
        })}
      </div>
    </Overlay>
  )
}

function StatusChip() {
  useTimeFormat()
  const { data: st } = useStatus()
  const stop = useStopRun()
  if (!st) return <span className="chip"><span className="dot" />…</span>
  const working = st.state === 'working'
  const label = { working: 'Working', asleep: 'Asleep', dormant: 'Dormant', paused: 'Paused' }[st.state]
  const text = working ? st.now : st.next_wakeup_at ? `next wakeup ${until(st.next_wakeup_at)}` : 'no wakeups planned'
  const budget = st.budget
    ? working && st.run_budget != null ? `${pts(st.run_used)} / ${pts(st.run_budget)} ${st.budget.unit}` : `${pts(st.budget.left)} ${st.budget.unit} left this ${st.budget.period ?? 'week'}`
    : usageText(st.usage)
  return <>
    <Link to={working && st.run_id ? `/runs/${st.run_id}` : '/runs'} className="chip" title="Open runs">
      <span className={`dot ${working ? 'working' : ''}`} />
      <span style={{ fontWeight: 500, flex: 'none' }}>{label}</span>
      <span className="muted">{text}</span>
      <span className="chip-sep" style={{ color: 'var(--dim)' }}>·</span>
      <span className="muted">{budget}</span>
    </Link>
    {working && st.run_id && (
      <button className="btn small danger" style={{ borderRadius: 999 }} disabled={stop.isPending} onClick={() => stop.mutate(st.run_id!)}>Stop</button>
    )}
  </>
}

function Sidebar() {
  const exts = useExtensions()
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
      <div className="side-label"><Link to="/general" className="side-head">General</Link><Link to="/new" title="New thread in General">+</Link></div>
      <GeneralThreads threads={general} currentId={currentId} />
      <div className="side-label">Projects <Link to="/projects" className="sub">All</Link></div>
      {active.map((p) => {
        const isOpen = open[p.slug] ?? (currentProject === p.slug || loc.pathname === `/p/${p.slug}`)
        const pts = (threads.data ?? []).filter((t) => t.project === p.slug)
        return (
          <div key={p.slug}>
            <div style={{ display: 'flex' }}>
              <button className="row" onClick={() => setOpen({ ...open, [p.slug]: !isOpen })} aria-expanded={isOpen}>
                <Caret open={isOpen} />
                <span className="grow">{p.name}</span>
                {p.unread > 0 && <span className="badge">{p.unread}</span>}
              </button>
              <Link to={`/p/${p.slug}`} className="sub" style={{ width: 'auto', padding: '6px 8px' }} title="Open project">›</Link>
            </div>
            {isOpen && pts.map((t) => <ThreadLink key={t.id} t={t} indent />)}
          </div>
        )
      })}
      {slotItems(exts.data, 'sidebar').map(({ ext, item }) => (
        <div key={`${ext.name}:${item.element}`} className="ext-side">
          <div className="side-label">{item.title ?? ext.title}</div>
          <ExtSlot ext={ext} element={item.element} context={{}} />
        </div>
      ))}
      <ChatList />
    </aside>
  )
}

/** Threads outside any project: the most recent few (plus the open one and any unread); the rest on the General screen. */
const GENERAL_SHOWN = 3
function GeneralThreads({ threads, currentId }: { threads: S['Thread'][]; currentId: number }) {
  const sorted = [...threads].sort((a, b) => Number(b.pinned) - Number(a.pinned) || Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at))
  const shown = sorted.filter((t, i) => i < GENERAL_SHOWN || t.id === currentId || t.unread > 0)
  const hidden = sorted.length - shown.length
  return <>
    {shown.map((t) => <ThreadLink key={t.id} t={t} indent={false} />)}
    {!sorted.length && <div className="sub" style={{ padding: '2px 10px' }}>No threads yet.</div>}
    {hidden > 0 && <Link to="/general" className="side-more" title={`See all ${sorted.length} General threads`} aria-label={`See all ${sorted.length} General threads`}>···</Link>}
  </>
}

/** The mockup's Chats list under the assistant's tree: grouped by day, newest first. */
function ChatList() {
  const chats = useChats()
  const groups = new Map<string, S['Chat'][]>()
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const today = day(new Date())
  for (const c of chats.data ?? []) {
    const t = day(new Date(c.updated_at))
    const label = t === today ? 'Today' : t === today - 86400_000 ? 'Yesterday' : t > today - 7 * 86400_000 ? 'This week' : 'Earlier'
    groups.set(label, [...(groups.get(label) ?? []), c])
  }
  return (
    <div className="side-chats">
      <div className="side-label">Chats <Link to="/new?chat=1" title="New chat (C)">+</Link></div>
      {!chats.data?.length && <div className="sub" style={{ padding: '2px 10px' }}>No chats yet.</div>}
      {[...groups].map(([label, list]) => (
        <div key={label}>
          <div className="side-group">{label}</div>
          {list.map((c) => (
            <NavLink key={c.id} to={`/c/${c.id}`} className={({ isActive }) => (isActive ? 'active' : '')}>
              <span className="grow">{c.title}</span>
              {c.busy && <span className="dot working" />}
            </NavLink>
          ))}
        </div>
      ))}
    </div>
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


/** While the owner has paused everything: on every screen, so it isn't forgotten. */
function PauseBanner() {
  useTimeFormat()
  const p = useStatus().data?.pause
  const resume = useResume()
  if (!p?.paused) return null
  return (
    <div className="banner pause-banner" role="status">
      <span><span style={{ fontWeight: 500 }}>Everything is paused</span> {p.until ? `until ${stamp(p.until)}` : 'until you resume'}</span>
      <span className="muted-text">No runs start; wakeups that come due are skipped.</span>
      <span style={{ flex: 1 }} />
      <Link to="/settings/system">Settings</Link>
      <button className="btn" disabled={resume.isPending} onClick={() => resume.mutate(undefined)}>Resume</button>
    </div>
  )
}

/** The mockup's update banner. Applying an update happens on the server (`./l0w update`, which
 * waits for the current run and don't-interrupt jobs), so the button shows how. Dismissed per version. */
function UpdateBanner() {
  const u = useUpdateInfo().data
  const key = `l0w-update-dismissed-${u?.latest}`
  const [hidden, setHidden] = useState(() => { try { return localStorage.getItem(key) === '1' } catch { return false } })
  const [how, setHow] = useState(false)
  const cancel = useCancelUpdate()
  useEffect(() => { try { setHidden(localStorage.getItem(key) === '1') } catch { /* private mode */ } }, [key])
  if (u?.draining_until) {
    return <div className="banner"><span style={{ fontWeight: 500 }}>Updating</span><span>Waiting for {u.waiting_for.length ? u.waiting_for.join('; ') : 'the assistant to be idle'}. No new runs start meanwhile.</span>
      <span style={{ flex: 1 }} />
      <button className="btn" disabled={cancel.isPending} onClick={() => cancel.mutate()} title="Clear this if ./l0w update isn't running any more">Cancel update</button>
    </div>
  }
  if (!u?.available || hidden) return null
  const summary = (u.notes ?? '').split('\n').map((l) => l.replace(/^[-#*\s]+/, '')).find((l) => l.trim()) ?? ''
  return (
    <div className="banner" role="status">
      <span style={{ fontWeight: 500 }}>l0wagent {u.latest} is available</span>
      {summary && <span className="ellipsis muted-text" style={{ minWidth: 0 }}>{summary}</span>}
      {u.url && <a href={u.url} target="_blank" rel="noreferrer">Release notes</a>}
      <span style={{ flex: 1 }} />
      {how
        ? <span>On the server, in the l0wagent folder: <code>./l0w update</code> (it waits for the current run)</span>
        : <button className="btn" onClick={() => setHow(true)}>Update after the current run</button>}
      <button className="x" aria-label="Dismiss" onClick={() => { try { localStorage.setItem(key, '1') } catch { /* private mode */ } setHidden(true) }}>✕</button>
    </div>
  )
}
