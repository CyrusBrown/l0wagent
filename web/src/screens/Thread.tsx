import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError, type S } from '../api/client'
import { problem, uploadFile, useDelivery, usePatchThread, usePostMessage, useProject, useProjects, useReplyNow, useThread, useThreadItems } from '../api/queries'
import { ClaimCard, PermissionCard, PromoteCard, QuestionCard, SuggestionCard, TaskCard } from '../components/Cards'
import { bytes, pts } from '../components/format'
import { useLinkPicker } from '../components/LinkPicker'
import { Markdown } from '../components/Markdown'
import { useOnline } from '../components/online'
import { api } from '../api/client'

export function Thread() {
  const id = Number(useParams().id)
  const thread = useThread(id)
  const items = useThreadItems(id)
  const scroller = useRef<HTMLDivElement>(null)
  const count = items.data?.items.length ?? 0

  // Mark read when opened and when new items arrive; keep scrolled to the bottom.
  useEffect(() => {
    if (!count) return
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
    if (thread.data?.unread) api.POST('/api/threads/{thread_id}/read', { params: { path: { thread_id: id } } })
  }, [id, count, thread.data?.unread])

  if (thread.isError) return <div className="page"><div className="error">{thread.error instanceof ApiError && thread.error.status === 404 ? 'This thread does not exist.' : "Couldn't load the thread."}</div></div>
  const t = thread.data
  return (
    <div className="thread">
      <div className="thread-scroll" ref={scroller}>
        <div className="thread-items">
          {t && <ThreadHead t={t} messages={(items.data?.items ?? []).filter((i) => i.type === 'message').length} />}
          {items.data?.items.map((it) => <Item key={`${it.type}-${itemId(it)}`} it={it} />)}
        </div>
      </div>
      {t && <Composer threadId={id} project={t.project} />}
    </div>
  )
}

function ThreadHead({ t, messages }: { t: S['Thread']; messages: number }) {
  const project = useProject(t.project ?? '')
  return (
    <div className="row-between" style={{ alignItems: 'flex-start' }}>
      <div className="stack" style={{ gap: 4, minWidth: 0 }}>
        <h1>{t.title}</h1>
        <div className="sub">
          {t.project ? <Link to={`/p/${t.project}`}>{project.data?.name ?? t.project}</Link> : 'General'}
          {' · '}{messages} messages · started {new Date(t.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          {t.status === 'archived' && ' · archived'}
        </div>
      </div>
      <ThreadMenu t={t} />
    </div>
  )
}

/** Owner actions on a thread (the agent can only suggest these). Not in the mockup; see REVIEW.md. */
function ThreadMenu({ t }: { t: S['Thread'] }) {
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'menu' | 'rename' | 'move'>('menu')
  const [title, setTitle] = useState(t.title)
  const patch = usePatchThread(t.id)
  const projects = useProjects('active')
  const done = () => { setOpen(false); setMode('menu') }
  return (
    <div style={{ position: 'relative', flex: 'none' }}>
      <button className="btn small" aria-label="Thread actions" onClick={() => setOpen(!open)}>⋯</button>
      {open && <>
        <div className="menu-scrim" onClick={done} />
        <div className="menu">
          {mode === 'menu' && <>
            <button onClick={() => setMode('rename')}>Rename</button>
            <button onClick={() => setMode('move')}>Move to project</button>
            <button onClick={() => { patch.mutate({ pinned: !t.pinned }); done() }}>{t.pinned ? 'Unpin' : 'Pin'}</button>
            <button onClick={() => { patch.mutate({ status: t.status === 'archived' ? 'open' : 'archived' }); done() }}>{t.status === 'archived' ? 'Unarchive' : 'Archive'}</button>
          </>}
          {mode === 'rename' && (
            <form className="stack" style={{ padding: 6 }} onSubmit={(e) => { e.preventDefault(); if (title.trim()) { patch.mutate({ title: title.trim() }); done() } }}>
              <input className="input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} />
              <div className="btns"><button className="btn small primary">Save</button></div>
            </form>
          )}
          {mode === 'move' && <>
            <button onClick={() => { patch.mutate({ project: '' }); done() }} disabled={!t.project}>General</button>
            {projects.data?.map((p) => <button key={p.slug} disabled={p.slug === t.project} onClick={() => { patch.mutate({ project: p.slug }); done() }}>{p.name}</button>)}
          </>}
        </div>
      </>}
    </div>
  )
}

const itemId = (it: S['ThreadItems']['items'][number]) => {
  switch (it.type) {
    case 'message': return it.message.id
    case 'question': return it.question.id
    case 'permission': return it.permission.id
    case 'task': return it.task.id
    case 'claim': return it.claim.id
    case 'suggestion': return it.suggestion.id
    case 'promote': return it.promote.id
  }
}

function Item({ it }: { it: S['ThreadItems']['items'][number] }) {
  switch (it.type) {
    case 'message': return <MessageView m={it.message} />
    case 'question': return <QuestionCard q={it.question} />
    case 'permission': return <PermissionCard p={it.permission} />
    case 'task': return <TaskCard t={it.task} />
    case 'claim': return <ClaimCard c={it.claim} />
    case 'suggestion': return <SuggestionCard s={it.suggestion} />
    case 'promote': return <PromoteCard p={it.promote} />
  }
}

function MessageView({ m }: { m: S['Message'] }) {
  if (m.sender === 'system') return <div className="sys">{m.body}</div>
  if (m.sender === 'assistant') return <div className="theirs"><Markdown>{m.body}</Markdown></div>
  return (
    <div className="mine">
      {m.sender === 'chat' && <div className="via">via chat</div>}
      <div className="bubble"><Markdown>{m.body}</Markdown></div>
      {m.attachments.map((a) => (
        <Link key={a.path} className="file-chip" style={{ color: 'var(--fg)', textDecoration: 'none' }}
          to={`/files?volume=${a.volume}&path=${encodeURIComponent(a.path.split('/').slice(0, -1).join('/'))}&file=${encodeURIComponent(a.name)}`}>
          <span className="ext">{a.name.split('.').pop()?.toUpperCase()}</span>{a.name}<span className="size">{bytes(a.size)}</span>
        </Link>
      ))}
      {m.delivery === 'queued' && <div className="via">queued</div>}
    </div>
  )
}

function Composer({ threadId, project }: { threadId: number; project: string | null }) {
  const [text, setText] = useState('')
  const [files, setFiles] = useState<{ key: string; name: string; upload?: S['Upload']; error?: string }[]>([])
  const post = usePostMessage(threadId)
  const delivery = useDelivery(threadId)
  const reply = useReplyNow(threadId)
  const online = useOnline()
  const picker = useRef<HTMLInputElement>(null)
  const [caret, setCaret] = useState(0)
  const links = useLinkPicker(text, setText, caret)
  const d = delivery.data
  const uploading = files.some((f) => !f.upload && !f.error)

  const attach = (list: FileList | File[]) => {
    for (const file of Array.from(list)) {
      const key = crypto.randomUUID()
      setFiles((fs) => [...fs, { key, name: file.name || 'pasted' }])
      uploadFile(file, project).then(
        (upload) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, upload } : f))),
        (e) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, error: problem(e) ?? 'failed' } : f))),
      )
    }
  }
  const send = () => {
    const body = text.trim()
    if (!body || uploading) return
    const upload_ids = files.flatMap((f) => (f.upload ? [f.upload.id] : []))
    post.mutate({ body, upload_ids }, { onSuccess: () => { setText(''); setFiles([]) } })
  }
  const note = !online ? 'Waiting for connection (on this device)' : d?.text ?? ''
  const dot = !online ? 'warn' : d?.state === 'working' ? 'ok' : ''
  return (
    <div className="composer-wrap">
      <div className="composer-stack">
        <div className="composer" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) attach(e.dataTransfer.files) }}>
          {!!files.length && (
            <div className="attach-row">
              {files.map((f) => (
                <span key={f.key} className={`file-chip ${f.error ? 'bad' : ''}`} title={f.error}>
                  <span className="ext">{f.name.split('.').pop()?.toUpperCase().slice(0, 4)}</span>{f.name}
                  <span className="size">{f.error ? 'failed' : f.upload ? bytes(f.upload.size) : 'uploading…'}</span>
                  <button className="link-btn" aria-label={`Remove ${f.name}`} onClick={() => setFiles((fs) => fs.filter((x) => x.key !== f.key))}>✕</button>
                </span>
              ))}
            </div>
          )}
          {links.menu}
          <textarea
            rows={2} placeholder="Reply… use @project, #thread or /file to link" value={text}
            onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart) }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
            onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files) } }}
            onKeyDown={(e) => { if (links.onKey(e)) return; if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send() }}
          />
          <div className="composer-foot">
            <button className="btn small ghost" onClick={() => picker.current?.click()} disabled={!online}>+ Attach</button>
            <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) attach(e.target.files); e.target.value = '' }} />
            {post.isError && <span style={{ color: 'var(--badInk)' }}>Not sent: {problem(post.error)}</span>}
            <button className="send" onClick={send} disabled={!text.trim() || post.isPending || uploading} title="Send (⌘↵)">↑</button>
          </div>
        </div>
        <div className="delivery">
          <span className="row-start"><span className={`dot ${dot}`} />{note}</span>
          {d?.reply_now_available && online && (
            <button className="link-btn accent" disabled={reply.isPending} onClick={() => reply.mutate()} title="Start a short reply run now">
              Reply now{d.reply_now_cost != null && <> · {pts(d.reply_now_cost)} pt <span className="sub">({pts(d.reply_budget_left)} left)</span></>}
            </button>
          )}
          {reply.isError && <span style={{ color: 'var(--badInk)' }}>{problem(reply.error)}</span>}
        </div>
      </div>
    </div>
  )
}
