import { useState } from 'react'
import { Link } from 'react-router'
import { useThreads } from '../api/queries'
import { ago } from '../components/format'

// General: every thread that isn't in a project, newest first (the sidebar shows the latest
// three). Laid out like the mockup's project list rows. Archived ones on their own tab.

export function General() {
  const [archived, setArchived] = useState(false)
  const [q, setQ] = useState('')
  const threads = useThreads({ general: true, archived })
  const list = (threads.data ?? [])
    .filter((t) => t.project === null && (!q.trim() || `${t.title} ${t.last_message_preview ?? ''}`.toLowerCase().includes(q.trim().toLowerCase())))
    .sort((a, b) => Number(b.pinned) - Number(a.pinned) || Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at))
  return (
    <div className="page" style={{ maxWidth: 1016, gap: 16 }}>
      <div className="row-between"><h1>General</h1><Link className="btn primary" to="/new" style={{ textDecoration: 'none' }}>+ New thread</Link></div>
      <div className="sub" style={{ fontSize: 13.5, marginTop: -6 }}>Threads that aren't part of a project. Move one into a project from its ⋯ menu.</div>
      <div className="row-between" style={{ flexWrap: 'wrap' }}>
        <div className="tabs" style={{ flex: 1 }}>
          <button className={!archived ? 'on' : ''} onClick={() => setArchived(false)}>Open</button>
          <button className={archived ? 'on' : ''} onClick={() => setArchived(true)}>Archived</button>
        </div>
      </div>
      <input className="input" placeholder="Search General threads" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search General threads" style={{ maxWidth: 320, background: 'var(--card)' }} />
      {threads.isSuccess && !list.length && <div className="empty">{q ? `No threads match “${q}”.` : archived ? 'No archived threads.' : 'No General threads yet.'}</div>}
      <div>
        {list.map((t) => (
          <Link key={t.id} to={`/t/${t.id}`} className="proj-row">
            <div className="stack" style={{ gap: 4, minWidth: 0 }}>
              <div className="row-start" style={{ gap: 9 }}><span style={{ font: '500 15px var(--sans)' }}>{t.pinned ? '📌 ' : ''}{t.title}</span></div>
              {t.last_message_preview && <span className="ellipsis" style={{ fontSize: 13.5, color: 'var(--muted)' }}>{t.last_message_preview}</span>}
            </div>
            <div className="stack" style={{ gap: 5, alignItems: 'flex-end', whiteSpace: 'nowrap' }}>
              <span className="sub">{ago(t.last_activity_at)}</span>
              <div className="row-start" style={{ gap: 6 }}>
                {!!t.unread && <span className="unread-pill">{t.unread} unread</span>}
                {!!t.open_asks && <span className="pill warn">{t.open_asks} waiting</span>}
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}
