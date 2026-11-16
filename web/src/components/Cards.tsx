import { createContext, useContext, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useAnswer, useDecidePermission, useSetSecret, useDecidePromote, useDecideSecurity, useDecideSuggestion, useResolveTask,
} from '../api/queries'
import { useOnline } from './online'
import { pts } from './format'

/** Where a card is shown. Home: the label reads "Question · #thread" (the mockup's Waiting on
 * you). Inbox: the label row is the thread (a link) and the time; the group title names the kind. */
export const CardPlace = createContext<{ mode: 'home' | 'inbox'; where?: ReactNode; when?: string } | null>(null)

/** The shared frame: label row, body, optional note ("+ Add comment"), and the decided state. */
function Card({ label, right, children, decided, actions, id }: {
  label: string; right?: ReactNode; children: ReactNode; decided?: ReactNode | false
  actions?: (note: string) => ReactNode; id?: string
}) {
  const [noteOpen, setNoteOpen] = useState(false)
  const [note, setNote] = useState('')
  const place = useContext(CardPlace)
  return (
    <div className={`card ${place ? `card-${place.mode}` : ''}`} id={id}>
      {place?.mode === 'inbox'
        ? <div className="card-label inbox"><span>{place.where}</span><span className="r">{right ? <>{right} · </> : null}{place.when}</span></div>
        : <div className="card-label"><span>{place?.mode === 'home' && place.where ? <>{label.split(' · ')[0]} · {place.where}</> : label}</span>{right && <span className="r">{right}</span>}</div>}
      {children}
      {decided ? decided : actions && (
        <>
          {noteOpen && <textarea className="textarea" rows={2} placeholder="A note for the assistant (optional)" value={note} onChange={(e) => setNote(e.target.value)} />}
          <div className="btns">
            {actions(note.trim())}
            <button className="link-btn" style={{ marginLeft: 'auto' }} onClick={() => { setNoteOpen(!noteOpen); if (noteOpen) setNote('') }}>
              {noteOpen ? 'Remove comment' : '+ Add comment'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function Decided({ text, note }: { text: string; note?: string | null }) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="done">✓ {text}</div>
      {note && <div className="note">Your comment · {note}</div>}
    </div>
  )
}

const qLabel: Record<S['Question']['kind'], string> = {
  yes_no: 'Question', single: 'Question · pick one', multi: 'Question · pick any', text: 'Question · written answer', slot: 'Question · pick a time', scale: 'Question · scale',
}

function show(v: unknown): string {
  if (v === true) return 'Yes'
  if (v === false) return 'No'
  if (Array.isArray(v)) return v.length ? v.join(', ') : 'none'
  return String(v)
}

export function QuestionCard({ q }: { q: S['Question'] }) {
  const answer = useAnswer()
  const [sel, setSel] = useState<string | null>(null)
  const [multi, setMulti] = useState<string[]>([])
  const [scale, setScale] = useState<number | null>(null)
  const [text, setText] = useState('')
  const send = (value: unknown, note: string) => answer.mutate({ id: q.id, value, note })
  const pending = answer.isPending
  const right = q.kind === 'multi' && q.status === 'open' ? (multi.length ? `${multi.length} selected` : 'select all that apply') : undefined

  const decided = q.status !== 'open' && <Decided text={q.status === 'answered' ? `You answered: ${show(q.answer)}` : q.status} note={q.note} />
  return (
    <Card id={`q${q.id}`} label={qLabel[q.kind]} right={right} decided={decided}
      actions={(note) => {
        switch (q.kind) {
          case 'yes_no':
            return <>
              <button className="btn primary" disabled={pending} onClick={() => send(true, note)}>Yes</button>
              <button className="btn" disabled={pending} onClick={() => send(false, note)}>No</button>
            </>
          case 'multi':
            return <button className="btn primary" disabled={pending || !multi.length} onClick={() => send(multi, note)}>Submit</button>
          case 'text':
            return <button className="btn primary" disabled={pending || !text.trim()} onClick={() => send(text.trim(), note)}>Send answer</button>
          case 'scale':
            return <button className="btn primary" disabled={pending || scale == null} onClick={() => send(scale, note)}>Submit</button>
          default:
            return <button className="btn primary" disabled={pending || !sel} onClick={() => send(sel, note)}>{q.kind === 'slot' ? 'Schedule' : 'Submit'}</button>
        }
      }}>
      <div className="card-q">{q.text}</div>
      {q.status === 'open' && <>
        {(q.kind === 'single' || q.kind === 'slot') && (
          <div className={q.kind === 'slot' ? 'opts-grid' : 'opts'}>
            {q.options.map((o) => (
              <button key={o} className={`opt ${sel === o ? 'on' : ''}`} onClick={() => setSel(o)}><span className="ring" />{o}</button>
            ))}
          </div>
        )}
        {q.kind === 'multi' && (
          <div className="opts-grid">
            {q.options.map((o) => {
              const on = multi.includes(o)
              return <button key={o} className={`opt ${on ? 'on' : ''}`} onClick={() => setMulti(on ? multi.filter((x) => x !== o) : [...multi, o])}>
                <span className="box">{on ? '✓' : ''}</span>{o}
              </button>
            })}
          </div>
        )}
        {q.kind === 'text' && <textarea className="textarea" rows={3} placeholder="Type your answer" value={text} onChange={(e) => setText(e.target.value)} />}
        {q.kind === 'scale' && (
          <div className="scale">
            <span>{q.scale_labels?.[0]}</span>
            {[1, 2, 3, 4, 5].map((n) => <button key={n} className={`opt ${scale === n ? 'on' : ''}`} onClick={() => setScale(n)}>{n}</button>)}
            <span>{q.scale_labels?.[1]}</span>
          </div>
        )}
      </>}
    </Card>
  )
}

const scopeLabel = { once: 'Allow once', project: 'Allow for project', always: 'Always allow' } as const

type SecretAction = { name: string; description?: string }
const secretActions = (p: S['Permission']): SecretAction[] =>
  p.actions.filter((a) => a.kind === 'secret').map((a) => a.payload as unknown as SecretAction)

/** Values typed into a card go straight to the write-only secrets endpoint, then the
 * permission is decided. They're never sent anywhere else. */
function useSecretInputs(p: S['Permission']) {
  const needed = secretActions(p)
  const [values, setValues] = useState<Record<string, string>>({})
  const setSecret = useSetSecret()
  const ready = needed.every((a) => (values[a.name] ?? '').trim())
  const save = async () => {
    for (const a of needed) await setSecret.mutateAsync({ name: a.name, value: values[a.name], description: a.description || null })
  }
  const fields = needed.map((a) => (
    <input key={a.name} className="input" type="password" autoComplete="off" aria-label={a.name}
      placeholder={needed.length > 1 ? `${a.name}${a.description ? ` · ${a.description}` : ''}` : 'Paste the value'}
      value={values[a.name] ?? ''} onChange={(e) => setValues({ ...values, [a.name]: e.target.value })} />
  ))
  return { needed, ready, save, fields, error: setSecret.error }
}

export function PermissionCard({ p }: { p: S['Permission'] }) {
  const decide = useDecidePermission()
  const online = useOnline()
  const secrets = useSecretInputs(p)
  if (secrets.needed.length && secrets.needed.length === p.actions.length) return <SecureInputCard p={p} />
  const decided = p.status !== 'open' && <Decided text={p.status === 'allowed' ? `Allowed${p.scope ? ` (${p.scope})` : ''}` : p.status === 'denied' ? 'Denied' : p.status} note={p.note} />
  const allow = async (scope: S['Permission']['scopes'][number], note: string) => {
    await secrets.save()
    decide.mutate({ id: p.id, decision: 'allow', scope, note: note || null })
  }
  const err = decide.error ?? secrets.error
  return (
    <Card id={`p${p.id}`} label="Permission" right="needs connection" decided={decided}
      actions={(note) => <>
        {[...p.scopes].reverse().map((scope, i) => (
          <button key={scope} className={`btn ${i === 0 ? 'primary' : ''}`} disabled={!online || decide.isPending || !secrets.ready}
            onClick={() => allow(scope, note)}>{scopeLabel[scope]}</button>
        ))}
        <button className="btn danger" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: p.id, decision: 'deny', note: note || null })}>Deny</button>
      </>}>
      <div className="card-text">{p.text}</div>
      {p.reason && <div className="card-sub">{p.reason}</div>}
      {p.actions.map((a, i) => <div key={i} className="card-sub">On approval: {a.summary}</div>)}
      {p.status === 'open' && secrets.fields}
      {err && <div className="error">{problem(err)}</div>}
    </Card>
  )
}

/** A permission whose only actions are secrets: the mockup's "Secure input" card. */
function SecureInputCard({ p }: { p: S['Permission'] }) {
  const decide = useDecidePermission()
  const online = useOnline()
  const secrets = useSecretInputs(p)
  const names = secrets.needed.map((a) => a.name)
  const decided = p.status !== 'open' && <Decided text={p.status === 'allowed' ? `Saved as ${names.join(', ')}` : p.status === 'denied' ? 'Declined' : p.status} note={p.note} />
  const scope = p.scopes.includes('always') ? 'always' : p.scopes[0]
  const err = decide.error ?? secrets.error
  return (
    <Card id={`p${p.id}`} label="Secure input" right="stored as a secret, never sent to a model" decided={decided}
      actions={(note) => <>
        <button className="btn primary" disabled={!online || !secrets.ready || decide.isPending}
          onClick={async () => { await secrets.save(); decide.mutate({ id: p.id, decision: 'allow', scope, note: note || null }) }}>Save</button>
        <button className="btn" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: p.id, decision: 'deny', note: note || null })}>Decline</button>
      </>}>
      <div style={{ fontSize: 14 }}>{p.text} {names.map((n) => <code key={n}>{n}</code>)}</div>
      {p.reason && <div className="card-sub">{p.reason}</div>}
      {p.status === 'open' && secrets.fields}
      {err && <div className="error">{problem(err)}</div>}
    </Card>
  )
}

export function TaskCard({ t }: { t: S['Task'] }) {
  const resolve = useResolveTask()
  const decided = t.status !== 'open' && <Decided text={t.status === 'done' ? 'Done' : t.status === 'declined' ? "Won't do" : t.status} note={t.note} />
  return (
    <Card id={`t${t.id}`} label="Task for you" decided={decided}
      actions={(note) => <>
        <button className="btn primary" disabled={resolve.isPending} onClick={() => resolve.mutate({ id: t.id, outcome: 'done', note })}>Done</button>
        <button className="btn" disabled={resolve.isPending} onClick={() => resolve.mutate({ id: t.id, outcome: 'declined', note })}>Won't do</button>
      </>}>
      <div className="card-text">{t.text}</div>
      {t.detail && <div className="card-sub">{t.detail}</div>}
    </Card>
  )
}

const sugLabel = (k: S['Suggestion']['kind']) => 'Suggestion · ' + k.split('_')[1]

export function SuggestionCard({ s }: { s: S['Suggestion'] }) {
  const decide = useDecideSuggestion()
  const decided = s.status !== 'open' && <Decided text={s.status === 'accepted' ? 'Accepted' : s.status === 'dismissed' ? 'Dismissed' : s.status} />
  return (
    <Card id={`s${s.id}`} label={sugLabel(s.kind)} decided={decided}
      actions={() => <>
        <button className="btn primary" disabled={decide.isPending} onClick={() => decide.mutate({ id: s.id, decision: 'accept' })}>Accept</button>
        <button className="btn" disabled={decide.isPending} onClick={() => decide.mutate({ id: s.id, decision: 'dismiss' })}>Dismiss</button>
      </>}>
      <div className="card-text">{s.text}</div>
    </Card>
  )
}

export function PromoteCard({ p }: { p: S['PromoteRequest'] }) {
  const decide = useDecidePromote()
  const online = useOnline()
  const decided = p.status !== 'open' && <Decided text={p.status === 'approved' ? 'Approved · continuing as a full run' : p.status === 'denied' ? 'Denied' : p.status} note={p.note} />
  return (
    <Card id={`pr${p.id}`} label="Promote request" right={`about ${pts(p.budget)} pts · runs now`} decided={decided}
      actions={(note) => <>
        <button className="btn primary" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: p.id, decision: 'approve', note })}>Approve</button>
        <button className="btn" disabled={!online || decide.isPending} onClick={() => decide.mutate({ id: p.id, decision: 'deny', note })}>Deny</button>
      </>}>
      <div className="stack" style={{ gap: 3 }}>
        <div style={{ fontWeight: 500 }}>Turn this reply into a full run</div>
        <div className="card-sub">{p.reason}</div>
      </div>
    </Card>
  )
}

export function SecurityCard({ a }: { a: S['SecurityAlert'] }) {
  const decide = useDecideSecurity()
  const decided = a.status !== 'open' && <Decided text={a.status === 'confirmed' ? 'Confirmed' : 'Revoked'} />
  return (
    <Card label="Security" decided={decided}
      actions={() => <>
        <button className="btn" disabled={decide.isPending} onClick={() => decide.mutate({ id: a.id, decision: 'confirm' })}>That was me</button>
        <button className="btn danger" disabled={decide.isPending} onClick={() => decide.mutate({ id: a.id, decision: 'revoke' })}>Revoke</button>
      </>}>
      <div className="card-text">{a.text}</div>
    </Card>
  )
}

export const claimPill: Record<S['Claim']['status'], string> = { planned: 'info', in_progress: 'run', done: 'ok', partial: 'warn', dropped: 'mute' }
export const claimText: Record<S['Claim']['status'], string> = { planned: 'Planned', in_progress: 'In progress', done: 'Done', partial: 'Partial', dropped: 'Dropped' }

export function ClaimCard({ c }: { c: S['Claim'] }) {
  return (
    <div className="claim-card">
      <div className="row-between">
        <div className="card-sub" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={`dot ${c.status === 'in_progress' ? 'working' : c.status === 'done' ? 'ok' : 'info'}`} />
          Planned in <Link to={`/runs/${c.run_id}`}>run #{c.run_id}</Link>
        </div>
        <span className={`pill ${claimPill[c.status]}`}>{claimText[c.status]}</span>
      </div>
      <div style={{ fontWeight: 500 }}>{c.plan}</div>
      {c.result && <div className="card-sub">{c.result}</div>}
    </div>
  )
}

/** Any inbox entry as its card. */
export function InboxCard({ e }: { e: S['InboxEntry'] }) {
  if (e.question) return <QuestionCard q={e.question} />
  if (e.permission) return <PermissionCard p={e.permission} />
  if (e.task) return <TaskCard t={e.task} />
  if (e.suggestion) return <SuggestionCard s={e.suggestion} />
  if (e.promote) return <PromoteCard p={e.promote} />
  if (e.security) return <SecurityCard a={e.security} />
  return null
}
