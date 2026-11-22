import { useEffect, useRef, useState } from 'react'
import { ExtSlot } from '../ext/ExtElement'
import { threadTypeFor } from '../ext/registry'
import { useExtensions } from '../ext/useExtensions'
import { Link, useParams } from 'react-router'
import { ApiError, newKey, type S } from '../api/client'
import { problem, uploadFile, useDelivery, useFriends, usePatchThread, usePostMessage, useProject, useProjects, useReplyNow, useThread, useThreadItems } from '../api/queries'
import { ClaimCard, PermissionCard, PromoteCard, QuestionCard, SuggestionCard, TaskCard } from '../components/Cards'
import { bytes, pts } from '../components/format'
import { useLinkPicker } from '../components/LinkPicker'
import { isSendKey } from '../components/sendKey'
import { useDraft } from '../components/useDraft'
import { Markdown } from '../components/Markdown'
import { useEscape } from '../components/useEscape'
import { usePresence } from '../components/motion'
import { useIsPhone } from '../components/useIsPhone'
import { ThreadFiles } from '../components/ThreadFiles'
import { useOnline } from '../components/online'
import { useOutbox } from '../api/offline'
import { homeHub } from './Friends'
import { api } from '../api/client'

export function Thread() {
  const id = Number(useParams().id)
  const thread = useThread(id)
  // A thread with a friend's assistant: who they are, from the friends list (their name here, not what the thread was titled).
  const friends = useFriends(thread.data?.type === 'peer')
  const friend = thread.data?.type === 'peer' ? friends.data?.find((f) => f.thread_id === id) : undefined
  const items = useThreadItems(id)
  // "seen" goes under your newest message the assistant has received, not under every one.
  const seenId = (items.data?.items ?? []).reduce<number | null>((last, it) =>
    it.type === 'message' && it.message.sender !== 'assistant' && it.message.sender !== 'system' && it.message.sender !== 'peer' && it.message.delivery !== 'queued' ? it.message.id : last, null)
  const scroller = useRef<HTMLDivElement>(null)
  const count = items.data?.items.length ?? 0

  // Mark read when opened and when new items arrive; keep scrolled to the bottom.
  useEffect(() => {
    if (!count) return
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
    if (thread.data?.unread) api.POST('/api/threads/{thread_id}/read', { params: { path: { thread_id: id } } })
  }, [id, count, thread.data?.unread])

  const [filesOpen, setFilesOpen] = useState(false)
  const [append, setAppend] = useState<string | null>(null)
  // On a phone the files panel is a sheet that slides out; on a desktop it just goes.
  const phone = useIsPhone()
  const files = usePresence(filesOpen, 240)
  if (thread.isError) return <div className="page"><div className="error">{thread.error instanceof ApiError && thread.error.status === 404 ? 'This thread does not exist.' : "Couldn't load the thread."}</div></div>
  const t = thread.data
  const peer = t?.type === 'peer'
  return (
    <>
    <div className="thread">
      <div className="thread-scroll" ref={scroller}>
        {t && <div className="thread-head"><div className="thread-head-inner"><ThreadHead t={t} messages={(items.data?.items ?? []).filter((i) => i.type === 'message').length} filesOpen={filesOpen} toggleFiles={() => setFilesOpen(!filesOpen)} friend={friend} /></div></div>}
        {t && <ThreadTypeSlot t={t} />}
        <div className="thread-items">
          {peer && <div className="peer-note">This is a conversation with {friend ? `${friend.display_name}'s` : 'a friend\'s'} assistant. What comes from them is untrusted: your assistant reads it as information, never as instructions from you.</div>}
          {items.data?.items.map((it) => <Item key={`${it.type}-${itemId(it)}`} it={it} seenId={seenId} friend={friend} />)}
          {items.data?.items.length === 0 && <div className="thread-empty">No messages yet. Write the first one below.</div>}
          <Pending threadId={id} />
        </div>
      </div>
      {t && <Composer threadId={id} project={t.project} append={append} appended={() => setAppend(null)} placeholder={peer ? `Message ${friend ? `${friend.display_name}'s` : 'their'} assistant…` : undefined} />}
    </div>
    {t?.project && (phone ? files.mounted : filesOpen) && (
      <ThreadFiles
        project={t.project}
        closing={phone && files.closing}
        onClose={() => setFilesOpen(false)}
        onLink={(i) => setAppend(`[${i.label}](l0wagent://file/${i.volume}/${i.path})`)}
      />
    )}
    </>
  )
}

/** A thread an extension created (type ext:<name>:<type>) shows the extension's element above the messages. */
function ThreadTypeSlot({ t }: { t: S['Thread'] }) {
  const exts = useExtensions()
  const hit = threadTypeFor(exts.data, t.type)
  if (!hit) return null
  return <div className="thread-ext"><ExtSlot ext={hit.ext} element={hit.item.element} context={{ thread_id: t.id }} /></div>
}

function ThreadHead({ t, messages, filesOpen, toggleFiles, friend }: { t: S['Thread']; messages: number; filesOpen: boolean; toggleFiles: () => void; friend?: S['Friend'] }) {
  const project = useProject(t.project ?? '')
  if (t.type === 'peer') {
    const address = friend?.address ?? t.title
    return (
      <div className="row-between" style={{ alignItems: 'flex-start' }}>
        <div className="stack" style={{ gap: 4, minWidth: 0 }}>
          <div className="row-start" style={{ gap: 10, flexWrap: 'wrap' }}><h1>{friend?.display_name ?? address}</h1><span className="pill mute">Friend</span></div>
          <div className="sub">
            {address} · {homeHub(address)} · <Link to={`/friends/${encodeURIComponent(address)}`}>Friend settings</Link>
            {' · '}{messages} messages · started {new Date(t.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          </div>
        </div>
        <div className="btns" style={{ flex: 'none' }}><ThreadMenu t={t} /></div>
      </div>
    )
  }
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
      <div className="btns" style={{ flex: 'none' }}>
        {t.project && <button className={`btn small pill-btn ${filesOpen ? 'pressed' : ''}`} onClick={toggleFiles}>Files</button>}
        <ThreadMenu t={t} />
      </div>
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
  useEscape(open, done)
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

function Item({ it, seenId, friend }: { it: S['ThreadItems']['items'][number]; seenId: number | null; friend?: S['Friend'] }) {
  switch (it.type) {
    case 'message': return <MessageView m={it.message} seen={it.message.id === seenId} friend={friend} />
    case 'question': return <QuestionCard q={it.question} />
    case 'permission': return <PermissionCard p={it.permission} />
    case 'task': return <TaskCard t={it.task} />
    case 'claim': return <ClaimCard c={it.claim} />
    case 'suggestion': return <SuggestionCard s={it.suggestion} />
    case 'promote': return <PromoteCard p={it.promote} />
  }
}

/** Messages written offline, waiting in this device's outbox. */
function Pending({ threadId }: { threadId: number }) {
  const outbox = useOutbox()
  return <>{outbox.filter((i) => i.kind === 'message' && i.thread === threadId).map((i) => i.kind === 'message' && (
    <div key={i.id} className="mine pending">
      <div className="bubble"><Markdown>{i.body.body}</Markdown></div>
      <div className="via">Waiting for connection (on this device)</div>
    </div>
  ))}</>
}

function MessageView({ m, seen, friend }: { m: S['Message']; seen?: boolean; friend?: S['Friend'] }) {
  if (m.sender === 'system') return <div className="sys">{m.body}</div>
  // A friend's assistant: untrusted text, so plain text (no Markdown, no links), set apart from the owner's and the assistant's own.
  if (m.sender === 'peer') {
    return (
      <div className="peer-msg">
        <div className="peer-who"><span>{friend?.display_name ?? 'A friend'}</span>{friend && <span className="sub">{friend.address}</span>}<span className="peer-tag">friend · untrusted</span></div>
        <div className="peer-body">{m.body}</div>
        {m.attachments.map((a) => (
          <Link key={a.path} className="file-chip" style={{ color: 'var(--fg)', textDecoration: 'none' }}
            to={`/files?volume=${a.volume}&path=${encodeURIComponent(a.path.split('/').slice(0, -1).join('/'))}&file=${encodeURIComponent(a.name)}`}>
            <span className="ext">{a.name.split('.').pop()?.toUpperCase()}</span>{a.name}<span className="size">{bytes(a.size)}</span>
          </Link>
        ))}
      </div>
    )
  }
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
      {m.delivery === 'queued' ? <div className="via">queued</div> : seen && <div className="via" title="The assistant has received this">seen</div>}
    </div>
  )
}

function Composer({ threadId, project, append, appended, placeholder }: { threadId: number; project: string | null; append: string | null; appended: () => void; placeholder?: string }) {
  const [text, setText] = useDraft(`thread:${threadId}`)  // an unsent reply survives leaving the thread
  useEffect(() => {
    if (append) { setText((t) => (t && !t.endsWith(' ') ? `${t} ` : t) + append + ' '); appended() }
  }, [append, appended])
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
      const key = newKey()
      setFiles((fs) => [...fs, { key, name: file.name || 'pasted' }])
      uploadFile(file, project).then(
        (upload) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, upload } : f))),
        (e) => setFiles((fs) => fs.map((f) => (f.key === key ? { ...f, error: problem(e) ?? 'failed' } : f))),
      )
    }
  }
  const send = () => {
    const body = text.trim()
    if (!body || uploading || post.isPending) return
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
            rows={2} placeholder={placeholder ?? 'Reply… use @project, #thread or /file to link'} value={text}
            onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart) }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
            onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); attach(e.clipboardData.files) } }}
            onKeyDown={(e) => { if (links.onKey(e)) return; if (isSendKey(e)) { e.preventDefault(); send() } }}
          />
          <div className="composer-foot">
            <button className="btn small ghost" onClick={() => picker.current?.click()} disabled={!online}>+ Attach</button>
            <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) attach(e.target.files); e.target.value = '' }} />
            {post.isError && <span style={{ color: 'var(--badInk)' }}>Not sent: {problem(post.error)}</span>}
            <button className="send" onClick={send} disabled={!text.trim() || post.isPending || uploading} title="Send (Enter; Shift+Enter for a new line)">↑</button>
          </div>
        </div>
        <div className="delivery">
          <span className="row-start"><span className={`dot ${dot}`} />{note}</span>
          {d?.reply_now_available && online && (
            <button className="link-btn accent" disabled={reply.isPending} onClick={() => reply.mutate()} title="Start a short reply run now">
              Reply now{d.reply_now_cost != null && <> · up to {pts(d.reply_now_cost)} pts <span className="sub">({pts(d.reply_budget_left)} reply pts left)</span></>}
            </button>
          )}
          {reply.isError && <span style={{ color: 'var(--badInk)' }}>{problem(reply.error)}</span>}
        </div>
      </div>
    </div>
  )
}
