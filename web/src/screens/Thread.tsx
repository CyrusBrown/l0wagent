import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError, type S } from '../api/client'
import { useDelivery, usePostMessage, useProject, useReplyNow, useThread, useThreadItems } from '../api/queries'
import { ClaimCard, PermissionCard, PromoteCard, QuestionCard, SuggestionCard, TaskCard } from '../components/Cards'
import { bytes, pts } from '../components/format'
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
      <Composer threadId={id} />
    </div>
  )
}

function ThreadHead({ t, messages }: { t: S['Thread']; messages: number }) {
  const project = useProject(t.project ?? '')
  return (
    <div className="stack" style={{ gap: 4 }}>
      <h1>{t.title}</h1>
      <div className="sub">
        {t.project ? <Link to={`/p/${t.project}`}>{project.data?.name ?? t.project}</Link> : 'General'}
        {' · '}{messages} messages · started {new Date(t.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
        {t.status === 'archived' && ' · archived'}
      </div>
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
      <div className="bubble">{m.body}</div>
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

function Composer({ threadId }: { threadId: number }) {
  const [text, setText] = useState('')
  const post = usePostMessage(threadId)
  const delivery = useDelivery(threadId)
  const reply = useReplyNow(threadId)
  const online = useOnline()
  const d = delivery.data
  const send = () => {
    const body = text.trim()
    if (!body) return
    post.mutate(body, { onSuccess: () => setText('') })
  }
  const note = !online ? 'Waiting for connection (on this device)' : d?.text ?? ''
  const dot = !online ? 'warn' : d?.state === 'working' ? 'ok' : ''
  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          rows={2} placeholder="Write to the assistant. Use @project, #thread or /file to link." value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send() }}
        />
        <div className="composer-foot">
          <span className={`dot ${dot}`} />
          <span>{note}</span>
          {d?.reply_now_available && online && (
            <button className="btn small" disabled={reply.isPending} onClick={() => reply.mutate()} title="Start a short reply run now">
              Reply now · {pts(d.reply_now_cost)} pt <span className="sub">({pts(d.reply_budget_left)} left)</span>
            </button>
          )}
          {post.isError && <span style={{ color: 'var(--badInk)' }}>Not sent</span>}
          <button className="send" onClick={send} disabled={!text.trim() || post.isPending} title="Send (⌘↵)">↑</button>
        </div>
      </div>
    </div>
  )
}
