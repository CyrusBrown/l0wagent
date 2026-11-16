import { useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import {
  problem, uploadChatFile, uploadFile, useCreateProject, useCreateChat, useCreateThread, useInbox, useMe, useProject, useProjects, useRuns, useShelf, useStatus, useThreads,
} from '../api/queries'
import { InboxCard } from '../components/Cards'
import { ago, bytes, duration, usageText } from '../components/format'
import { ShelfView } from '../components/ShelfView'

const statusPill = { active: 'ok', stale: 'warn', archived: 'mute' } as const
const statusText = { active: 'Active', stale: 'Stale', archived: 'Archived' } as const

export function Projects() {
  const [status, setStatus] = useState<S['Project']['status'] | undefined>()
  const [creating, setCreating] = useState(false)
  const projects = useProjects(status)
  return (
    <div className="page" style={{ maxWidth: 960, gap: 16 }}>
      <div className="row-between"><h1>Projects</h1>{!creating && <button className="btn primary" onClick={() => setCreating(true)}>+ New project</button>}</div>
      {creating && <NewProject onDone={() => setCreating(false)} />}
      <div className="pills">
        {([[undefined, 'All'], ['active', 'Active'], ['stale', 'Stale'], ['archived', 'Archived']] as const).map(([k, l]) => (
          <button key={l} className={`filter ${status === k ? 'on' : ''}`} onClick={() => setStatus(k)}>{l}</button>
        ))}
      </div>
      {projects.isSuccess && projects.data.length === 0 && (
        <div className="placeholder">
          <div style={{ font: '500 18px var(--serif)' }}>No projects yet</div>
          <div className="sub" style={{ fontSize: 13.5, maxWidth: 420 }}>Projects group threads and files around one goal. The assistant creates them as work grows, or start one here.</div>
        </div>
      )}
      <div>
        {projects.data?.map((p) => (
          <Link key={p.slug} to={`/p/${p.slug}`} className="proj-row" style={{ opacity: p.status === 'archived' ? 0.6 : 1 }}>
            <div className="stack" style={{ gap: 4, minWidth: 0 }}>
              <div className="row-start" style={{ gap: 9, flexWrap: 'wrap' }}><span style={{ font: '500 17px var(--serif)' }}>{p.name}</span><span className={`pill ${statusPill[p.status]}`}>{statusText[p.status]}</span></div>
              {p.summary && <span style={{ fontSize: 13.5, color: 'var(--muted)' }}>{p.summary}</span>}
            </div>
            <div className="stack" style={{ gap: 5, alignItems: 'flex-end', whiteSpace: 'nowrap' }}>
              <span className="sub">{ago(p.last_activity_at)}</span>
              <div className="row-start" style={{ gap: 6 }}>
                {!!p.unread && <span className="unread-pill">{p.unread} unread</span>}
                {!!p.open_asks && <span className="pill warn">{p.open_asks} waiting</span>}
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}

function NewProject({ onDone }: { onDone: () => void }) {
  const create = useCreateProject()
  const nav = useNavigate()
  const [f, setF] = useState({ name: '', summary: '' })
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); create.mutate(f, { onSuccess: (p) => nav(`/p/${p.slug}`) }) }}>
      <div className="card-label"><span>New project</span></div>
      <input className="input" autoFocus placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
      <textarea className="textarea" rows={2} placeholder="What it's about (the assistant keeps the project doc)" value={f.summary} onChange={(e) => setF({ ...f, summary: e.target.value })} />
      {create.error && <div className="error">{problem(create.error)}</div>}
      <div className="btns"><button className="btn primary" disabled={!f.name.trim() || create.isPending}>Create</button><button type="button" className="btn" onClick={onDone}>Cancel</button></div>
    </form>
  )
}

type Tab = 'overview' | 'threads' | 'files' | 'asks' | 'activity'

export function Project() {
  const slug = useParams().slug!
  const nav = useNavigate()
  const project = useProject(slug)
  const [showArchived, setShowArchived] = useState(false)
  const threads = useThreads({ project: slug, archived: true })
  const shelf = useShelf(slug)
  const waiting = useInbox('waiting')
  const runs = useRuns(undefined, slug)
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') ?? 'overview') as Tab
  const setTab = (t: Tab) => setParams(t === 'overview' ? {} : { tab: t })
  if (project.isError) return <div className="page"><div className="error">No such project.</div></div>
  const p = project.data
  if (!p) return <div className="page" />
  const asks = (waiting.data ?? []).filter((e) => e.project === slug)
  const all = threads.data ?? []
  const archived = all.filter((t) => t.status === 'archived')
  const shown = showArchived ? all : all.filter((t) => t.status !== 'archived')
  const lastRun = runs.data?.[0]
  const tabs: [Tab, string][] = [['overview', 'Overview'], ['threads', 'Threads'], ['files', 'Files'], ['asks', asks.length ? `Asks · ${asks.length}` : 'Asks'], ['activity', 'Activity']]
  return (
    <div className="page" style={{ maxWidth: 1040, gap: 20 }}>
      <div className="page-head" style={{ flexWrap: 'wrap' }}>
        <div className="stack" style={{ gap: 6 }}>
          <div className="row-start" style={{ gap: 10 }}><h1 style={{ fontSize: 28 }}>{p.name}</h1><span className={`pill ${statusPill[p.status]}`}>{statusText[p.status]}</span></div>
          <div className="sub" style={{ fontSize: 13 }}>
            {p.thread_count} threads · {shelf.data?.length ?? 0} files · {p.open_asks} open asks{lastRun ? ` · last run ${ago(lastRun.started_at)}` : ''}
          </div>
        </div>
        <button className="btn" onClick={() => nav(`/new?project=${slug}`)}>New thread in project</button>
      </div>
      <div className="tabs">
        {tabs.map(([k, label]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}</button>)}
      </div>
      {tab === 'overview' && (
        <div className="proj-overview">
          <div className="stack" style={{ gap: 14 }}>
            <div className="section-label">Project doc · kept by the assistant</div>
            {p.doc_path
              ? <div className="sub" style={{ fontSize: 13.5 }}>The assistant keeps it at <code>{p.doc_path}</code> in its memory. Reading it here arrives with the files browser (M3).</div>
              : <div className="sub" style={{ fontSize: 13.5 }}>No project doc yet. The assistant writes one in its memory when it starts working on the project.</div>}
          </div>
          <div className="stack" style={{ gap: 18 }}>
            <div className="stack" style={{ gap: 8 }}><div className="section-label">Summary</div><div style={{ fontSize: 13.5, lineHeight: 1.55, color: 'var(--fg2)' }}>{p.summary || '–'}</div></div>
            {!!p.links.length && (
              <div className="stack" style={{ gap: 6 }}><div className="section-label">Links</div>
                {p.links.map((l) => <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer nofollow" style={{ fontSize: 13.5 }}>{l.label}</a>)}
              </div>
            )}
          </div>
        </div>
      )}
      {tab === 'threads' && (
        <div>
          <div className="row-between" style={{ padding: '0 4px 8px' }}>
            <span className="sub" style={{ fontSize: 13 }}>{all.length - archived.length} active threads</span>
            {!!archived.length && (
              <button className="toggle" onClick={() => setShowArchived(!showArchived)} aria-pressed={showArchived}>
                <span className={`switch ${showArchived ? 'on' : ''}`}><span /></span>Show archived ({archived.length})
              </button>
            )}
          </div>
          {shown.length === 0 && <div className="empty">No threads yet.</div>}
          {shown.map((t) => (
            <Link key={t.id} to={`/t/${t.id}`} className="thread-row" style={{ opacity: t.status === 'archived' ? 0.6 : 1 }}>
              <div className="stack" style={{ gap: 3, minWidth: 0, flex: 1 }}>
                <div className="row-start"><span style={{ font: `${t.unread ? 600 : 500} 14.5px var(--sans)` }}>{t.title}</span>{t.status === 'archived' && <span className="pill mute">Archived</span>}</div>
                {t.last_message_preview && <span className="ellipsis" style={{ fontSize: 13, color: 'var(--muted)' }}>{t.last_message_preview}</span>}
              </div>
              <span className="sub" style={{ flex: 'none' }}>{ago(t.last_activity_at)}</span>
            </Link>
          ))}
        </div>
      )}
      {tab === 'files' && <ShelfView project={slug} />}
      {tab === 'asks' && (asks.length === 0 ? <div className="empty">Nothing waiting on you in this project.</div> : (
        <div className="stack" style={{ maxWidth: 720 }}>
          {asks.map((e) => <div key={e.kind + e.id} className="stack" style={{ gap: 4 }}>{e.thread_id && <Link className="sub" to={`/t/${e.thread_id}`}>#{e.thread_title}</Link>}<InboxCard e={e} /></div>)}
        </div>
      ))}
      {tab === 'activity' && (runs.data?.length === 0 ? <div className="empty">No runs have touched this project yet.</div> : (
        <table className="table">
          <thead><tr><th>#</th><th>About</th><th>Started</th><th>Status</th></tr></thead>
          <tbody>{runs.data?.map((r) => (
            <tr key={r.id} className="click" onClick={() => nav(`/runs/${r.id}`)}>
              <td className="sub">#{r.id}</td><td>{r.about ?? r.why_detail ?? r.kind}</td><td className="sub">{ago(r.started_at)} · {duration(r.started_at, r.ended_at)}</td><td><span className="pill mute">{r.status}</span></td>
            </tr>
          ))}</tbody>
        </table>
      ))}
    </div>
  )
}

function greeting(d = new Date()) {
  const h = d.getHours()
  return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'
}

/** "New", from the mockup: one composer that starts a thread (General, or a project via
 * ?project=). The 1:1 chat side arrives with M6. */
export function NewItem() {
  const [params] = useSearchParams()
  const project = params.get('project')
  const projectInfo = useProject(project ?? '')
  const me = useMe()
  const status = useStatus()
  const waiting = useInbox('waiting')
  const create = useCreateThread()
  const newChat = useCreateChat()
  const nav = useNavigate()
  const picker = useRef<HTMLInputElement>(null)
  const [text, setText] = useState('')
  const [chat, setChat] = useState(params.get('chat') === '1' && !project)
  const [files, setFiles] = useState<S['Upload'][]>([])
  const [chatFiles, setChatFiles] = useState<S['ChatFile'][]>([])
  const [uploading, setUploading] = useState(0)
  const st = status.data
  const attach = async (list: FileList) => {
    for (const f of Array.from(list)) {
      setUploading((n) => n + 1)
      try {
        if (chat) { const u = await uploadChatFile(f); setChatFiles((fs) => [...fs, u]) } else { const u = await uploadFile(f, project); setFiles((fs) => [...fs, u]) }
      } finally { setUploading((n) => n - 1) }
    }
  }
  const submit = () => {
    const body = text.trim()
    if (!body || uploading) return
    if (chat) {
      newChat.mutate({ message: body, upload_ids: chatFiles.map((f) => f.id) }, { onSuccess: (c) => c && nav(`/c/${c.id}`) })
      return
    }
    create.mutate({ title: body.split('\n')[0].slice(0, 80), project, first_message: body, upload_ids: files.map((f) => f.id) }, { onSuccess: (t) => nav(t ? `/t/${t.id}` : project ? `/p/${project}` : '/') })  // null: queued offline, sent when back online
  }
  const where = project ? (projectInfo.data?.name ?? project) : 'General'
  return (
    <div className="new-page">
      <h1 className="new-greeting">{greeting()}{me.data?.display_name ? `, ${me.data.display_name}` : ''}</h1>
      <div className="stack" style={{ gap: 10 }}>
        <div className="new-composer">
          <textarea rows={3} autoFocus placeholder={chat ? 'Start a chat. Replies stream right away.' : project ? `Start a thread in ${where}…` : 'What should the assistant work on?'} value={text}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit() }} />
          {!!(chat ? chatFiles : files).length && <div className="attach-row">{(chat ? chatFiles : files).map((f) => <span key={f.id} className="file-chip"><span className="ext">{f.name.split('.').pop()?.toUpperCase().slice(0, 4)}</span>{f.name}<span className="size">{bytes(f.size)}</span></span>)}</div>}
          <div className="row-start" style={{ gap: 8, flexWrap: 'wrap' }}>
            <button className="btn square" title="Attach files" onClick={() => picker.current?.click()}>+</button>
            <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) void attach(e.target.files); e.target.value = '' }} />
            <div className="seg">
              <button className={chat ? 'on' : ''} disabled={!!project} onClick={() => setChat(true)} title="A 1:1 chat with live replies">1:1 chat</button>
              <button className={chat ? '' : 'on'} onClick={() => setChat(false)}>Assistant · {where}</button>
            </div>
            <span style={{ flex: 1 }} />
            <button className="send" onClick={submit} disabled={!text.trim() || create.isPending || newChat.isPending || !!uploading} title={chat ? 'Start the chat (⌘↵)' : 'Start the thread (⌘↵)'}>↑</button>
          </div>
        </div>
        <div className="row-start sub" style={{ padding: '0 8px', fontSize: 12.5, color: 'var(--muted)' }}>
          <span className={`dot ${st?.state === 'working' ? 'ok' : ''}`} />
          {chat ? 'A 1:1 chat with your own model. It can read the assistant\'s threads and files, and hand work over to it.'
            : st?.state === 'working' ? 'The assistant is working now and sees new threads after its next step.' : 'Starts a thread. The assistant picks it up on its next run, or press Reply now in the thread.'}
        </div>
        {(create.error || newChat.error) && <div className="error">{problem(create.error ?? newChat.error)}</div>}
      </div>
      <div className="new-chips">
        <Link to={st?.run_id ? `/runs/${st.run_id}` : '/runs'} className="new-chip">
          <span className="section-label">Assistant</span>
          <span className="row-start" style={{ font: '500 14px var(--sans)' }}><span className={`dot ${st?.state === 'working' ? 'working' : ''}`} />{st ? { working: 'Working', asleep: 'Asleep', dormant: 'Dormant', paused: 'Paused' }[st.state] : '…'}</span>
          <span className="ellipsis sub" style={{ color: 'var(--muted)' }}>{st?.state === 'working' ? st.now : 'no wakeups planned'}</span>
        </Link>
        <div className="new-chip">
          <span className="section-label">{st?.budget ? `Budget this ${st.budget.period ?? 'week'}` : 'Claude usage this week'}</span>
          <span style={{ font: '500 14px var(--sans)' }}>{st?.budget ? <>{st.budget.left} <span style={{ fontWeight: 400, color: 'var(--muted)' }}>of {st.budget.allowance} {st.budget.unit} left</span></> : usageText(st?.usage)}</span>
          <div className="gauge"><div style={{ width: `${Math.min(100, st?.budget ? (1 - st.budget.left / st.budget.allowance) * 100 : st?.usage?.seven_day_pct ?? 0)}%` }} /></div>
        </div>
        {st?.budget && (
          <div className="new-chip">
            <span className="section-label">You vs. assistant</span>
            <span style={{ font: '500 14px var(--sans)' }}>{Math.round(st.budget.used_by_owner + st.budget.used_by_chat)} <span style={{ fontWeight: 400, color: 'var(--muted)' }}>/ {Math.round(st.budget.used_by_assistant)} pts</span></span>
            <div className="split-bar2" style={{ height: 4 }}>
              <div style={{ width: `${(100 * (st.budget.used_by_owner + st.budget.used_by_chat)) / Math.max(1, st.budget.used_by_owner + st.budget.used_by_chat + st.budget.used_by_assistant)}%`, background: 'var(--infoDot)' }} />
              <div style={{ flex: 1, background: 'var(--accent)' }} />
            </div>
          </div>
        )}
        <Link to="/inbox" className="new-chip">
          <span className="section-label">Waiting on you</span>
          <span style={{ font: '500 14px var(--sans)' }}>{waiting.data?.length ?? 0} <span style={{ fontWeight: 400, color: 'var(--muted)' }}>items</span></span>
          <span style={{ fontSize: 12, color: 'var(--accentInk)' }}>Open Inbox</span>
        </Link>
      </div>
    </div>
  )
}
