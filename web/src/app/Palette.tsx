import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { useProjects, useSearch, useThreads } from '../api/queries'
import { routeFor } from '../components/format'
import { useEscape } from '../components/useEscape'

type Hit = { group: string; title: string; sub?: string; to: string }

/** ⌘K: jump to a thread, project, file or run, or start an action. Server search fills in as you type. */
export function Palette({ onClose }: { onClose: () => void }) {
  const [q, setQ] = useState('')
  const [debounced, setDebounced] = useState('')
  const [sel, setSel] = useState(0)
  const nav = useNavigate()
  const threads = useThreads()
  const projects = useProjects()
  const search = useSearch(debounced)
  const prevFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)
  useEscape(true, onClose)
  useEffect(() => () => { const el = prevFocus.current; if (el?.isConnected) el.focus() }, [])

  useEffect(() => {
    const t = setTimeout(() => setDebounced(q), 150)
    return () => clearTimeout(t)
  }, [q])

  const hits = useMemo<Hit[]>(() => {
    const ql = q.toLowerCase()
    const match = (s: string) => !ql || s.toLowerCase().includes(ql)
    const actions: Hit[] = [
      { group: 'Actions', title: 'New thread or chat', sub: 'N', to: '/new' },
      { group: 'Actions', title: 'Open Inbox', to: '/inbox' },
      { group: 'Actions', title: 'Browse files', to: '/files' },
      { group: 'Actions', title: 'New project', to: '/projects' },
      { group: 'Actions', title: 'Runs · wake the assistant now', to: '/runs' },
      { group: 'Actions', title: 'Tools & Secrets', to: '/tools' },
      { group: 'Actions', title: 'Settings', to: '/settings' },
    ].filter((h) => match(h.title))
    const local: Hit[] = [
      ...(threads.data ?? []).filter((t) => match(t.title)).slice(0, 6).map((t) => ({ group: 'Threads', title: t.title, sub: t.project ?? 'General', to: `/t/${t.id}` })),
      ...(projects.data ?? []).filter((p) => match(p.name)).slice(0, 4).map((p) => ({ group: 'Projects', title: p.name, sub: p.status, to: `/p/${p.slug}` })),
    ]
    const seen = new Set(local.map((h) => h.to))
    const remote: Hit[] = (debounced === q ? search.data ?? [] : [])
      .map((h) => ({ group: { thread: 'Threads', message: 'Messages', project: 'Projects', file: 'Files', run: 'Runs', shelf: 'Files' }[h.kind], title: h.title, sub: h.snippet ?? undefined, to: routeFor(h.link) ?? '' }))
      .filter((h) => h.to && !seen.has(h.to))
    return [...actions, ...local, ...remote]
  }, [q, debounced, threads.data, projects.data, search.data])

  const go = (h: Hit | undefined) => {
    if (!h) return
    nav(h.to)
    onClose()
  }

  const groups = [...new Set(hits.map((h) => h.group))]
  return (
    <div className="scrim" onClick={onClose}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Search" onClick={(e) => e.stopPropagation()}>
        <div className="palette-top">
        <input
          className="palette-input" enterKeyHint="search" autoComplete="off"
          autoFocus placeholder="Search threads, messages, projects, files, runs…" value={q}
          onChange={(e) => { setQ(e.target.value); setSel(0) }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, hits.length - 1)) }
            if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
            if (e.key === 'Enter') go(hits[sel])
          }}
        />
        <button className="palette-cancel" onClick={onClose}>Cancel</button>
        </div>
        <div className="results">
          {hits.length === 0 && <div className="empty" style={{ margin: 8 }}>{search.isFetching ? 'Searching…' : 'Nothing found'}</div>}
          {groups.map((g) => (
            <div key={g}>
              <div className="group">{g}</div>
              {hits.map((h, i) => h.group === g && (
                <button key={h.group + h.to + h.title} className={`hit ${i === sel ? 'sel' : ''}`} onMouseEnter={() => setSel(i)} onClick={() => go(h)}>
                  <span>{h.title}</span>{h.sub && <small>{h.sub}</small>}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
