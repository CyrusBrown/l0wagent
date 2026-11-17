import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import type { S } from '../api/client'
import { useDraft } from '../api/live'
import {
  problem, useChat, useChatMessages, useChatPull, useDecideChatCard, useDelivery, usePatchChat, useProviderCatalog, useProviders,
  uploadChatFile, useReplyNow, useSendChat, useStopChat, useThreads,
} from '../api/queries'
import { useOnline } from '../components/online'
import { CodeBox } from '../components/CopyCode'
import { useTimeFormat } from '../app/timeFormat'
import { bytes, stamp } from '../components/format'
import { Markdown } from '../components/Markdown'
import { joinTier, modelChoices, splitTier, tierLabel } from '../components/models'
import { Select } from '../components/Select'
import { useEscape } from '../components/useEscape'

// The mockup's chat screen (M6): a header with the hand-off actions, the conversation (your
// bubbles, the model's serif replies, one quiet line for its tool use, cards for anything
// sensitive), and the composer with the model picker. Replies stream as they're written.

const cap = (x: string) => x[0].toUpperCase() + x.slice(1)

export function Chat() {
  useTimeFormat()
  const id = Number(useParams().id)
  const chat = useChat(id)
  const msgs = useChatMessages(id)
  const draft = useDraft(id)
  const send = useSendChat(id)
  const online = useOnline()
  const stop = useStopChat(id)
  const [text, setText] = useState('')
  const [files, setFiles] = useState<S['ChatFile'][]>([])
  const [uploading, setUploading] = useState(0)
  const [upErr, setUpErr] = useState<string | null>(null)
  const picker = useRef<HTMLInputElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const attach = async (list: FileList | File[]) => {
    setUpErr(null)
    for (const f of Array.from(list)) {
      setUploading((n) => n + 1)
      try { const u = await uploadChatFile(f); setFiles((fs) => [...fs, u]) } catch (e) { setUpErr(problem(e)) } finally { setUploading((n) => n - 1) }
    }
  }
  const c = chat.data
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight }) }, [msgs.data?.length, draft])
  if (chat.isError) return <div className="page"><div className="error">{problem(chat.error)}</div></div>
  if (!c) return null
  const submit = () => {
    const body = text.trim() || (files.length ? 'See the attached files.' : '')
    if (!body || c.busy || uploading || !online) return
    send.mutate({ body, upload_ids: files.map((f) => f.id) }, { onSuccess: () => { setText(''); setFiles([]) } })
  }
  return (
    <div className="thread">
      <ChatHeader chat={c} />
      <div className="thread-scroll" ref={scroller}>
        <div className="thread-items" role="log" aria-live="polite" aria-relevant="additions">
          <Items chatId={id} items={msgs.data ?? []} />
          {c.busy && (draft ? <div className="theirs" aria-busy="true"><Markdown>{draft}</Markdown></div> : <div className="tool-line" role="status">Thinking…</div>)}
        </div>
      </div>
      <div className="composer-wrap">
        <div className="composer-stack">
          <div className="composer" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) void attach(e.dataTransfer.files) }}>
            <textarea rows={2} placeholder="Message" value={text} aria-label="Message" onChange={(e) => setText(e.target.value)}
              onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); void attach(e.clipboardData.files) } }}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() } }} />
            {(files.length > 0 || uploading > 0) && (
              <div className="attach-row">
                {files.map((f) => (
                  <span key={f.id} className="file-chip"><span className="ext">{f.name.split('.').pop()?.toUpperCase().slice(0, 4)}</span>{f.name}
                    <button className="link-btn" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x.id !== f.id))}>✕</button></span>
                ))}
                {uploading > 0 && <span className="sub">Uploading…</span>}
              </div>
            )}
            <div className="composer-foot">
              <button className="btn small ghost" onClick={() => picker.current?.click()} title="Attach files (or paste / drop them)">+ Attach</button>
              <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) void attach(e.target.files); e.target.value = '' }} aria-label="Attach files" />
              <ModelPicker chat={c} />
              <span style={{ flex: 1 }} />
              {c.busy
                ? <button className="btn small" onClick={() => stop.mutate()}>Stop</button>
                : <button className="send" onClick={submit} disabled={(!text.trim() && !files.length) || send.isPending || uploading > 0 || !online} title="Send (↵)">↑</button>}
            </div>
          </div>
          {!online && <div className="sub" role="status">Chat needs a connection; thread messages can be queued.</div>}
          {(send.error || upErr) && <div className="error">{upErr ?? problem(send.error)}</div>}
        </div>
      </div>
    </div>
  )
}

function ChatHeader({ chat }: { chat: S['Chat'] }) {
  const [open, setOpen] = useState<'pull' | null>(null)
  const thread = chat.handed_off_to
  const delivery = useDelivery(thread?.id ?? 0)
  const reply = useReplyNow(thread?.id ?? 0)
  const nav = useNavigate()
  const d = thread ? delivery.data : undefined
  useEscape(open !== null, () => setOpen(null))
  return (
    <div className="chat-head">
      <span className="chat-title">{chat.title}</span>
      <div className="btns" style={{ position: 'relative' }}>
        <button className="btn small" onClick={() => setOpen(open === 'pull' ? null : 'pull')} title="Give this chat a thread as read-only context">Pull in a thread</button>
        {d?.reply_now_available && (
          <button className="btn small" disabled={reply.isPending} onClick={() => reply.mutate(undefined, { onSuccess: (r) => r && nav(`/runs/${r.id}`) })}>
            Wake the assistant{d.reply_now_cost != null ? ` · ~${d.reply_now_cost} pts` : ''}
          </button>
        )}
        {open && <>
          <div className="menu-scrim" onClick={() => setOpen(null)} />
          <div className="menu chat-pop">
            {open === 'pull' && <PullForm chat={chat} done={() => setOpen(null)} />}
          </div>
        </>}
      </div>
    </div>
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

/** Any model from any connected provider: Claude's names on Claude providers, and an opencode
 * provider's whole catalog (the models enabled for the assistant first). Effort is its own picker. */
function ModelPicker({ chat }: { chat: S['Chat'] }) {
  const providers = useProviders()
  const patch = usePatchChat(chat.id)
  const usable = providers.data?.filter((p) => p.enabled && p.has_credential) ?? []
  const go = usable.find((p) => p.kind === 'opencode-go')
  const catalog = useProviderCatalog(go?.id ?? 'opencode-go', !!go)
  const [model, effort] = splitTier(chat.tier)
  const groups = usable.map((p) => {
    if (!p.models) return { p, models: modelChoices(p) }
    const enabled = modelChoices(p)
    const rest = (catalog.data ?? []).filter((m) => !m.enabled).map((m) => ({
      id: m.id, label: m.name, efforts: m.variants ?? [], price: m.input === 0 && m.output === 0 ? 'free' : `$${m.input ?? '?'} / $${m.output ?? '?'} per M`,
    }))
    return { p, models: [...enabled, ...rest] }
  })
  const cur = groups.find((g) => g.p.id === chat.provider)?.models.find((m) => m.id === model)
  const efforts = cur?.efforts ?? []
  const pick = (value: string) => {
    const [pid, mid] = value.split('|')
    const m = groups.find((g) => g.p.id === pid)?.models.find((x) => x.id === mid)
    const e = effort && m?.efforts.includes(effort) ? effort : m?.efforts.includes('medium') ? 'medium' : m?.efforts[0] ?? null
    patch.mutate({ provider: pid, tier: joinTier(mid, e) })
  }
  return (
    <span className="row-start" style={{ gap: 2 }}>
      <Select variant="pick" value={`${chat.provider}|${model}`} label="Chat model"
        options={[
          ...(!cur ? [{ value: `${chat.provider}|${model}`, label: tierLabel(chat.tier, usable.find((p) => p.id === chat.provider)) }] : []),
          ...groups.flatMap(({ p, models }) => models.map((m) => ({ value: `${p.id}|${m.id}`, label: `${m.label}${m.price ? ` · ${m.price}` : ''}`, group: p.label }))),
        ]}
        onChange={pick} />
      {efforts.length > 0 && (
        <Select variant="pick" value={effort ?? ''} label="Chat effort"
          options={efforts.map((e) => ({ value: e, label: e }))}
          onChange={(v) => patch.mutate({ tier: joinTier(model, v || null) })} />
      )}
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
    if (m.role === 'owner') {
      const fs = (m.data?.files ?? []) as S['ChatFile'][]
      out.push(
        <div key={m.id} className="mine">
          <div className="bubble">{m.body}</div>
          {fs.map((f) => (
            <a key={f.id} className="file-chip" href={`/api/chat-files/${f.id}`} download={f.name}>
              <span className="ext">{f.name.split('.').pop()?.toUpperCase().slice(0, 4)}</span>{f.name}<span className="size">{bytes(f.size)}</span>
            </a>
          ))}
        </div>,
      )
    }
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
  propose_egress_rule: 'proposed a network rule', propose_integration: 'proposed an integration',
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
          <CodeBox text={String(t.data?.result ?? '')}><pre>{String(t.data?.result ?? '').slice(0, 2000)}</pre></CodeBox>
        </div>
      ))}
    </div>
  )
}

function SystemNote({ m }: { m: S['ChatMessage'] }) {
  const tid = m.data?.thread_id as number | undefined
  return <div className="sys">{tid ? <Link to={`/t/${tid}`}>{m.body}</Link> : m.body}</div>
}

const CARD_TITLE: Record<string, string> = {
  secret: 'Secure input', wakeup: 'Extra wakeup', level: 'Budget level', egress: 'Network rule', settings: 'Settings change', handoff: 'Hand off to the assistant', integration: 'Connect integration',
}
const HANDOFF_TARGET: Record<string, string> = { new_thread: 'a new thread', new_project: 'a new project', existing_thread: 'an existing thread' }

export function ChatCard({ chatId, m }: { chatId: number; m: S['ChatMessage'] }) {
  const decide = useDecideChatCard(chatId)
  const [value, setValue] = useState('')
  const d = (m.data ?? {}) as Record<string, string | number | null | undefined>
  const kind = String(d.kind)
  const open = d.state === 'open'
  const right = !open ? (d.state === 'done' ? 'done' : 'declined') : kind === 'secret' || kind === 'integration' ? 'stored as a secret, never sent to a model' : 'needs your approval'
  const act = (decision: 'approve' | 'decline') => decide.mutate({ message_id: m.id, decision, value: decision === 'approve' && (kind === 'secret' || kind === 'integration') && value ? value : null })
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
      {kind === 'integration' && <>
        <div className="card-text"><b>{d.title}</b> · <code>{d.url}</code></div>
        {d.guidance && <div className="card-sub">{d.guidance}</div>}
        {d.reason && <div className="card-sub">{d.reason}</div>}
        {d.token_url && <a className="card-sub" href={String(d.token_url)} target="_blank" rel="noreferrer">Open the token page ↗</a>}
        {open && <>
          <div className="inline-edit">
            <input className="input" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Token" aria-label={`Token for ${d.title}`} />
            <button className="btn primary" disabled={decide.isPending} onClick={() => act('approve')}>Connect</button>
            <button className="btn" disabled={decide.isPending} onClick={() => act('decline')}>Decline</button>
          </div>
          <span className="card-sub">Leave empty to use the saved {d.secret_name}</span>
        </>}
        {d.state === 'done' && (d.status === 'ok'
          ? <div className="card-text" style={{ color: 'var(--okInk)' }}>✓ Connected {d.title} · {d.tool_count} tools</div>
          : <div className="error">Saved the token, but {d.title} answered: {d.last_error}</div>)}
      </>}
      {kind === 'wakeup' && <div className="card-text">Wake the assistant {d.at ? `at ${stamp(String(d.at))}` : 'soon'} with up to {d.budget} pts: {d.reason}</div>}
      {kind === 'level' && <div className="card-text">Switch to the <b>{cap(String(d.level))}</b> level{d.reason ? `: ${d.reason}` : '.'}</div>}
      {kind === 'settings' && (
        <div className="stack" style={{ gap: 6 }}>
          {(m.data?.changes as { label: string; before: string; after: string }[] | undefined)?.map((c, i) => (
            <div key={i} className="change-line"><span>{c.label}</span><span className="mono"><s>{c.before}</s> → <b>{c.after}</b></span></div>
          ))}
          {d.reason && <span className="card-sub">{d.reason}</span>}
        </div>
      )}
      {kind === 'handoff' && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="card-text">
            To {d.target === 'existing_thread' ? <>the thread <b>#{d.thread_title}</b></> : d.target === 'new_project' ? <>a new project <b>{d.project_name}</b>, thread <b>{d.title}</b></> : <>a new thread <b>{d.title}</b>{d.project ? <> in {d.project}</> : null}</>}
            {' · '}{d.mode === 'transcript' ? 'with this whole chat attached' : 'as a message'}
          </div>
          <div className="handoff-msg">{String(d.message ?? '')}</div>
          {d.state === 'done' && d.thread_id && <Link className="card-sub" to={`/t/${d.thread_id}`}>Open #{d.thread_title ?? d.title} →</Link>}
          {d.state === 'open' && <span className="card-sub">Goes to {HANDOFF_TARGET[String(d.target)] ?? 'the assistant'}; the assistant picks it up at its next run.</span>}
        </div>
      )}
      {kind === 'egress' && <div className="card-text">{d.action === 'block' ? 'Block' : 'Allow'} <code>{d.host}</code> for the assistant{d.reason ? `: ${d.reason}` : '.'}</div>}
      {open && kind !== 'secret' && kind !== 'integration' && <div className="btns">
        <button className="btn primary" disabled={decide.isPending} onClick={() => act('approve')}>Approve</button>
        <button className="btn" disabled={decide.isPending} onClick={() => act('decline')}>Decline</button>
      </div>}
      {decide.error && <div className="error">{problem(decide.error)}</div>}
    </div>
  )
}
