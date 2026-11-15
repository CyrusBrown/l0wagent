import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import type { S } from '../api/client'
import { useCreateThread, useProject, useProjects, useShelf, useThreads } from '../api/queries'
import { ago, bytes } from '../components/format'

const statusPill = { active: 'ok', stale: 'warn', archived: 'mute' } as const

export function Projects() {
  const [status, setStatus] = useState<S['Project']['status'] | undefined>()
  const projects = useProjects(status)
  return (
    <div className="page narrow">
      <h1>Projects</h1>
      <div className="pills">
        {([[undefined, 'All'], ['active', 'Active'], ['stale', 'Stale'], ['archived', 'Archived']] as const).map(([k, l]) => (
          <button key={l} className={`filter ${status === k ? 'on' : ''}`} onClick={() => setStatus(k)}>{l}</button>
        ))}
      </div>
      {projects.isSuccess && projects.data.length === 0 && <div className="empty">No projects yet: the assistant creates them as work grows.</div>}
      {projects.data?.map((p) => (
        <Link key={p.slug} to={`/p/${p.slug}`} className="card" style={{ color: 'var(--fg)', textDecoration: 'none', opacity: p.status === 'archived' ? 0.7 : 1 }}>
          <div className="row-between">
            <span style={{ font: '500 17px var(--serif)' }}>{p.name}</span>
            <span className={`pill ${statusPill[p.status]}`}>{p.status}</span>
          </div>
          <div className="card-sub">{p.summary}</div>
          <div className="sub">{p.thread_count} threads{p.unread ? ` · ${p.unread} unread` : ''}{p.open_asks ? ` · ${p.open_asks} open asks` : ''} · {ago(p.last_activity_at)}</div>
        </Link>
      ))}
    </div>
  )
}

export function Project() {
  const slug = useParams().slug!
  const project = useProject(slug)
  const threads = useThreads({ project: slug, archived: true })
  const shelf = useShelf(slug)
  const [tab, setTab] = useState<'overview' | 'threads' | 'files'>('overview')
  if (project.isError) return <div className="page"><div className="error">No such project.</div></div>
  const p = project.data
  if (!p) return <div className="page" />
  return (
    <div className="page narrow">
      <div className="stack" style={{ gap: 4 }}>
        <h1>{p.name}</h1>
        <div className="sub"><span className={`pill ${statusPill[p.status]}`}>{p.status}</span> · {p.thread_count} threads · {ago(p.last_activity_at)}</div>
      </div>
      <div className="tabs">
        {(['overview', 'threads', 'files'] as const).map((t) => (
          <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)} style={{ textTransform: 'capitalize' }}>{t}{t === 'files' ? ` · ${shelf.data?.length ?? 0}` : ''}</button>
        ))}
      </div>
      {tab === 'overview' && (
        <div className="stack">
          <div className="theirs">{p.summary}</div>
          {p.doc_path && <div className="sub">Project doc: <Link to={`/files?volume=workspace&path=${encodeURIComponent(p.doc_path.split('/').slice(0, -1).join('/'))}&file=${encodeURIComponent(p.doc_path.split('/').pop()!)}`}>{p.doc_path}</Link></div>}
        </div>
      )}
      {tab === 'threads' && threads.data?.map((t) => (
        <Link key={t.id} to={`/t/${t.id}`} className="card" style={{ color: 'var(--fg)', textDecoration: 'none', gap: 4, opacity: t.status === 'archived' ? 0.7 : 1 }}>
          <div className="row-between"><span style={{ fontWeight: t.unread ? 600 : 500 }}>{t.title}</span><span className="sub">{ago(t.last_activity_at)}</span></div>
          {t.last_message_preview && <div className="card-sub">{t.last_message_preview}</div>}
        </Link>
      ))}
      {tab === 'files' && (
        shelf.data?.length === 0 ? <div className="empty">Nothing on the shelf yet. The assistant shelves deliverables here; you can add files from the file browser.</div> : (
          <table className="table">
            <thead><tr><th>Item</th><th>Mode</th><th>By</th><th>Updated</th></tr></thead>
            <tbody>{shelf.data?.map((i) => (
              <tr key={i.id}>
                <td><div>{i.label}</div><div className="sub"><code>{i.path}</code> · {bytes(i.size)}</div></td>
                <td><span className={`pill ${i.mode === 'live' ? 'ok' : 'mute'}`}>{i.mode}</span>{i.versions > 1 && <span className="sub"> v{i.versions}</span>}</td>
                <td className="sub">{i.added_by === 'owner' ? 'From you' : 'Assistant'}</td>
                <td className="sub">{ago(i.updated_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        )
      )}
    </div>
  )
}

/** "New": start a thread (General or a project). Chats arrive in M6. */
export function NewItem() {
  const projects = useProjects('active')
  const create = useCreateThread()
  const nav = useNavigate()
  const [project, setProject] = useState<string | null>(null)
  const [text, setText] = useState('')
  const submit = () => {
    const body = text.trim()
    if (!body) return
    create.mutate({ title: body.split('\n')[0].slice(0, 80), project, first_message: body }, { onSuccess: (t) => nav(`/t/${t.id}`) })
  }
  return (
    <div className="page narrow">
      <h1>New thread</h1>
      <div className="sub">The assistant works on threads during runs. 1:1 chats with live replies come with M6.</div>
      <div className="pills">
        <button className={`filter ${project === null ? 'on' : ''}`} onClick={() => setProject(null)}>General</button>
        {projects.data?.map((p) => <button key={p.slug} className={`filter ${project === p.slug ? 'on' : ''}`} onClick={() => setProject(p.slug)}>{p.name}</button>)}
      </div>
      <textarea className="textarea" rows={5} autoFocus placeholder="What should the assistant work on?" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit() }} />
      <div className="btns"><button className="btn primary" disabled={!text.trim() || create.isPending} onClick={submit}>Start thread</button></div>
    </div>
  )
}
