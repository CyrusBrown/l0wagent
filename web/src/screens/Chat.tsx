import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import type { S } from '../api/client'
import { useDraft } from '../api/live'
import {
  problem, useChat, useChatAsk, useChatHandoff, useChatMessages, useChatPull, useDecideChatCard, useDelivery, usePatchChat, useProjects, useProviders,
  useReplyNow, useSendChat, useStopChat, useThreads,
} from '../api/queries'
import { Markdown } from '../components/Markdown'

// The mockup's chat screen (M6): a header with the hand-off actions, the conversation (your
// bubbles, the model's serif replies, one quiet line for its tool use, cards for anything
// sensitive), and the composer with the model picker. Replies stream as they're written.

const TIERS = ['haiku', 'sonnet', 'opus'].flatMap((m) => ['low', 'medium', 'high'].map((e) => `${m}/${e}`))
const cap = (x: string) => x[0].toUpperCase() + x.slice(1)
const tierLabel = (t: string) => t.split('/').map(cap).join(' · ')

export function Chat() {
  const id = Number(useParams().id)
  const chat = useChat(id)
  const msgs = useChatMessages(id)
  const draft = useDraft(id)
  const send = useSendChat(id)
  const stop = useStopChat(id)
  const [text, setText] = useState('')
  const scroller = useRef<HTMLDivElement>(null)
  const c = chat.data
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }) }, [msgs.data?.length, draft])
  if (chat.isError) return <div className="page"><div className="error">{problem(chat.error)}</div></div>
  if (!c) return null
  const submit = () => {
    const body = text.trim()
    if (!body || c.busy) return
    send.mutate(body, { onSuccess: () => setText('') })
  }
  return (
    <div className="thread">
      <ChatHeader chat={c} />
      <div className="thread-scroll" ref={scroller}>
        <div className="thread-items">
          <Items chatId={id} items={msgs.data ?? []} />
          {c.busy && (draft ? <div className="theirs"><Markdown>{draft}</Markdown></div> : <div className="tool-line">Thinking…</div>)}
        </div>
      </div>
      <div className="composer-wrap">
        <div className="composer-stack">
          <div className="composer">
            <textarea rows={2} placeholder="Message" value={text} aria-label="Message" onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() } }} />
            <div className="composer-foot">
              <ModelPicker chat={c} />
              <span style={{ flex: 1 }} />
              {c.busy
                ? <button className="btn small" onClick={() => stop.mutate()}>Stop</button>
                : <button className="send" onClick={submit} disabled={!text.trim() || send.isPending} title="Send (↵)">↑</button>}
            </div>
          </div>
          {send.error && <div className="error">{problem(send.error)}</div>}
        </div>
      </div>
    </div>
  )
}

function ChatHeader({ chat }: { chat: S['Chat'] }) {
  const [open, setOpen] = useState<'move' | 'ask' | 'pull' | null>(null)
  const thread = chat.handed_off_to
  const delivery = useDelivery(thread?.id ?? 0)
  const reply = useReplyNow(thread?.id ?? 0)
  const nav = useNavigate()
  const d = thread ? delivery.data : undefined
  return (
    <div className="chat-head">
      <span className="chat-title">{chat.title}</span>
      <div className="btns" style={{ position: 'relative' }}>
        <button className="btn small" onClick={() => setOpen(open === 'move' ? null : 'move')}>Move to the assistant…</button>
        <button className="btn small" onClick={() => setOpen(open === 'ask' ? null : 'ask')}>Ask the assistant</button>
        <button className="btn small" onClick={() => setOpen(open === 'pull' ? null : 'pull')} title="Give this chat a thread as read-only context">Pull in a thread</button>
        {d?.reply_now_available && (
          <button className="btn small" disabled={reply.isPending} onClick={() => reply.mutate(undefined, { onSuccess: (r) => r && nav(`/runs/${r.id}`) })}>
            Wake the assistant{d.reply_now_cost != null ? ` · ~${d.reply_now_cost} pts` : ''}
          </button>
        )}
        {open && <>
          <div className="menu-scrim" onClick={() => setOpen(null)} />
          <div className="menu chat-pop">
            {open === 'move' && <MoveForm chat={chat} done={() => setOpen(null)} />}
            {open === 'ask' && <AskForm chat={chat} done={() => setOpen(null)} />}
            {open === 'pull' && <PullForm chat={chat} done={() => setOpen(null)} />}
          </div>
        </>}
      </div>
    </div>
  )
}

function MoveForm({ chat, done }: { chat: S['Chat']; done: () => void }) {
  const projects = useProjects()
  const move = useChatHandoff(chat.id)
  const [title, setTitle] = useState(chat.title)
  const [project, setProject] = useState('')
  return (
    <form className="stack" style={{ gap: 8, padding: 6 }} onSubmit={(e) => { e.preventDefault(); move.mutate({ title, project: project || null }, { onSuccess: done }) }}>
      <span className="sub">A new thread for the assistant: a summary of this chat, with the transcript attached.</span>
      <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Thread title" />
      <select className="input" value={project} onChange={(e) => setProject(e.target.value)} aria-label="Project">
        <option value="">General</option>
        {projects.data?.filter((p) => p.status !== 'archived').map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
      </select>
      {move.error && <div className="error">{problem(move.error)}</div>}
      <div><button className="btn small primary" disabled={!title.trim() || move.isPending}>Move</button></div>
    </form>
  )
}

function AskForm({ chat, done }: { chat: S['Chat']; done: () => void }) {
  const threads = useThreads()
  const ask = useChatAsk(chat.id)
  const [thread, setThread] = useState<number>(chat.handed_off_to?.id ?? 0)
  const [message, setMessage] = useState('')
  return (
    <form className="stack" style={{ gap: 8, padding: 6 }} onSubmit={(e) => { e.preventDefault(); ask.mutate({ thread_id: thread, message }, { onSuccess: done }) }}>
      <select className="input" value={thread} onChange={(e) => setThread(Number(e.target.value))} aria-label="Thread">
        <option value={0} disabled>Pick a thread</option>
        {threads.data?.map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
      </select>
      <textarea className="input" rows={3} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Your message to the assistant" aria-label="Message to the assistant" />
      {ask.error && <div className="error">{problem(ask.error)}</div>}
      <div><button className="btn small primary" disabled={!thread || !message.trim() || ask.isPending}>Send</button></div>
    </form>
  )
}

function PullForm({ chat, done }: { chat: S['Chat']; done: () => void }) {
  const threads = useThreads()
  const pull = useChatPull(chat.id)
  return (
    <>
      {threads.data?.filter((t) => !(chat.context_threads ?? []).includes(t.id)).slice(0, 30).map((t) => (
        <button key={t.id} onClick={() => pull.mutate(t.id, { onSuccess: done })}>{t.title}</button>
      ))}
      {pull.error && <div className="error">{problem(pull.error)}</div>}
    </>
  )
}

function ModelPicker({ chat }: { chat: S['Chat'] }) {
  const providers = useProviders()
  const patch = usePatchChat(chat.id)
  const usable = providers.data?.filter((p) => p.enabled) ?? []
  const mapped = usable.find((p) => p.id === chat.provider)?.models
  return (
    <span className="row-start" style={{ gap: 4 }}>
      {usable.length > 1 && (
        <select className="model-pick" value={chat.provider} aria-label="Chat provider" onChange={(e) => patch.mutate({ provider: e.target.value })}>
          {usable.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        </select>
      )}
      <select className="model-pick" value={chat.tier} aria-label="Chat model" onChange={(e) => patch.mutate({ tier: e.target.value })}>
        {(TIERS.includes(chat.tier) ? TIERS : [chat.tier, ...TIERS]).map((t) => {
          const m = mapped?.[t.split('/')[0]]?.model
          return <option key={t} value={t}>{m ? `${m.replace(/^opencode-go\//, '')} · ${t.split('/')[1]}` : tierLabel(t)}</option>
        })}
      </select>
    </span>
  )
}

/** The conversation, with consecutive tool calls folded into one quiet line. */
function Items({ chatId, items }: { chatId: number; items: S['ChatMessage'][] }) {
  const out: React.ReactNode[] = []
  let tools: S['ChatMessage'][] = []
  const flush = () => {
    if (tools.length) out.push(<ToolLine key={`t${tools[0].id}`} tools={tools} />)
    tools = []
  }
  for (const m of items) {
    if (m.role === 'tool') { tools.push(m); continue }
    flush()
    if (m.role === 'owner') out.push(<div key={m.id} className="mine"><div className="bubble">{m.body}</div></div>)
    else if (m.role === 'assistant') { if (m.body.trim()) out.push(<div key={m.id} className="theirs"><Markdown>{m.body}</Markdown></div>) }
    else if (m.role === 'card') out.push(<ChatCard key={m.id} chatId={chatId} m={m} />)
    else out.push(<SystemNote key={m.id} m={m} />)
  }
  flush()
  return <>{out}</>
}

const LABELS: Record<string, string> = {
  status: 'checked the status', list_projects: 'listed projects', list_threads: 'listed threads', read_thread: 'read a thread', search: 'searched',
  list_runs: 'listed runs', read_run: 'read a run', shelf: 'checked the shelf', wakeup_plan: 'checked the wakeup plan', list_files: 'browsed the workspace',
  read_file: 'read a file', create_thread: 'started a thread', post_to_thread: 'posted to a thread', create_project: 'created a project',
  reply_now: 'woke the assistant', ask_for_secret: 'asked for a secret', propose_wakeup: 'proposed a wakeup', propose_level: 'proposed a level',
  propose_egress_rule: 'proposed a network rule',
}

function ToolLine({ tools }: { tools: S['ChatMessage'][] }) {
  const [open, setOpen] = useState(false)
  const counts = new Map<string, number>()
  tools.forEach((t) => { const n = String(t.data?.name); counts.set(n, (counts.get(n) ?? 0) + 1) })
  const text = [...counts].map(([n, k]) => {
    const l = LABELS[n] ?? n
    return k > 1 ? (l.startsWith('read a ') ? `read ${k} ${l.slice(7)}s` : `${l} ×${k}`) : l
  }).join(' · ')
  const failed = tools.some((t) => t.data?.is_error)
  return (
    <div className="stack" style={{ gap: 6 }}>
      <button className="tool-line" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-rule" />{cap(text)}{failed && <span style={{ color: 'var(--badInk)' }}> · a tool failed</span>}
      </button>
      {open && tools.map((t) => (
        <div key={t.id} className="tool-detail">
          <code>{String(t.data?.name)}({JSON.stringify(t.data?.input)})</code>
          <pre>{String(t.data?.result ?? '').slice(0, 2000)}</pre>
        </div>
      ))}
    </div>
  )
}

function SystemNote({ m }: { m: S['ChatMessage'] }) {
  const tid = m.data?.thread_id as number | undefined
  return <div className="sys">{tid ? <Link to={`/t/${tid}`}>{m.body}</Link> : m.body}</div>
}

const CARD_TITLE: Record<string, string> = { secret: 'Secure input', wakeup: 'Extra wakeup', level: 'Budget level', egress: 'Network rule' }

function ChatCard({ chatId, m }: { chatId: number; m: S['ChatMessage'] }) {
  const decide = useDecideChatCard(chatId)
  const [value, setValue] = useState('')
  const d = (m.data ?? {}) as Record<string, string | number | null | undefined>
  const kind = String(d.kind)
  const open = d.state === 'open'
  const right = !open ? (d.state === 'done' ? 'done' : 'declined') : kind === 'secret' ? 'stored as a secret, never sent to a model' : 'needs your approval'
  const act = (decision: 'approve' | 'decline') => decide.mutate({ message_id: m.id, decision, value: decision === 'approve' && kind === 'secret' ? value : null })
  return (
    <div className="card" aria-label={CARD_TITLE[kind] ?? 'Card'}>
      <div className="card-label"><span>{CARD_TITLE[kind] ?? kind}</span><span className="r">{right}</span></div>
      {kind === 'secret' && <>
        <div className="card-text"><code>{d.name}</code>{d.description ? ` · ${d.description}` : ''}</div>
        {d.link && <a className="card-sub" href={String(d.link)} target="_blank" rel="noreferrer">Where to get it ↗</a>}
        {open && <div className="inline-edit">
          <input className="input" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Value" aria-label={`Value for ${d.name}`} />
          <button className="btn primary" disabled={!value || decide.isPending} onClick={() => act('approve')}>Save</button>
          <button className="btn" disabled={decide.isPending} onClick={() => act('decline')}>Decline</button>
        </div>}
      </>}
      {kind === 'wakeup' && <div className="card-text">Wake the assistant {d.at ? `at ${new Date(String(d.at)).toLocaleString()}` : 'soon'} with up to {d.budget} pts: {d.reason}</div>}
      {kind === 'level' && <div className="card-text">Switch to the <b>{cap(String(d.level))}</b> level{d.reason ? `: ${d.reason}` : '.'}</div>}
      {kind === 'egress' && <div className="card-text">{d.action === 'block' ? 'Block' : 'Allow'} <code>{d.host}</code> for the assistant{d.reason ? `: ${d.reason}` : '.'}</div>}
      {open && kind !== 'secret' && <div className="btns">
        <button className="btn primary" disabled={decide.isPending} onClick={() => act('approve')}>Approve</button>
        <button className="btn" disabled={decide.isPending} onClick={() => act('decline')}>Decline</button>
      </div>}
      {decide.error && <div className="error">{problem(decide.error)}</div>}
    </div>
  )
}
