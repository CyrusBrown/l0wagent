import { useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import { useAcceptAll, useInbox, useInboxCounts } from '../api/queries'
import { useTimeFormat } from '../app/timeFormat'
import { CardPlace, InboxCard } from '../components/Cards'
import { ago, clock } from '../components/format'
import { useOnline } from '../components/online'

// The Inbox, ported from the mockup's INBOX screen: Waiting (grouped by kind, each card with
// its thread and time) and History (past decisions, searchable, with their outcome).

type Kind = S['InboxEntry']['kind']
const FILTERS: [string, Kind[] | null][] = [
  ['All', null], ['Security', ['security']], ['Questions', ['question']], ['Permissions', ['permission', 'task']], ['Suggestions', ['suggestion']], ['Promote', ['promote']],
]
const GROUPS: [string, Kind[], boolean][] = [
  ['Security', ['security'], false],  // first: a sign-in you don't recognize matters most (owner, 2026-11-17)
  ['Questions', ['question'], false], ['Permissions & tasks', ['permission', 'task'], false], ['Thread suggestions', ['suggestion'], true],
  ['Promote requests', ['promote'], false],
]
const KIND: Record<Kind, string> = { question: 'Question', permission: 'Permission', task: 'Task', suggestion: 'Suggestion', promote: 'Promote', security: 'Security' }

function outcome(e: S['InboxEntry']): [string, string] {
  const st = (e.question ?? e.permission ?? e.task ?? e.suggestion ?? e.promote ?? e.security)?.status ?? ''
  const label: Record<string, [string, string]> = {
    answered: ['Answered', 'info'], allowed: ['Allowed', 'ok'], denied: ['Denied', 'bad'], done: ['Done', 'ok'], declined: ['Declined', 'mute'],
    accepted: ['Accepted', 'ok'], dismissed: ['Dismissed', 'mute'], approved: ['Approved', 'ok'], withdrawn: ['Withdrawn', 'mute'], expired: ['Expired', 'mute'],
    confirmed: ['That was me', 'ok'], revoked: ['Revoked', 'bad'],
  }
  return label[st] ?? [st, 'mute']
}

function text(e: S['InboxEntry']): string {
  return e.question?.text ?? e.permission?.text ?? e.task?.text ?? e.suggestion?.text ?? e.promote?.reason ?? e.security?.text ?? ''
}

function note(e: S['InboxEntry']): string | null | undefined {
  return e.question?.note ?? e.permission?.note ?? e.task?.note ?? e.promote?.note
}

function when(iso: string): string {
  const d = new Date(iso)
  const days = (Date.now() - d.getTime()) / 86400_000
  if (days < 1) return d.toDateString() === new Date().toDateString() ? `Today ${clock(iso)}` : ago(iso)
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'short' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const where = (e: S['InboxEntry']) => (e.thread_id ? <Link to={`/t/${e.thread_id}`}>#{e.thread_title}</Link> : <span>{e.kind === 'security' ? 'Account' : 'Settings'}</span>)

export function Inbox() {
  useTimeFormat()
  const [tab, setTab] = useState<'waiting' | 'history'>('waiting')
  const [filter, setFilter] = useState(0)
  const [q, setQ] = useState('')
  const counts = useInboxCounts()
  const list = useInbox(tab, undefined, tab === 'history' ? q : undefined)
  const acceptAll = useAcceptAll()
  const online = useOnline()
  const waiting = counts.data?.waiting ?? 0
  const kinds = FILTERS[filter][1]
  const items = (list.data ?? []).filter((e) => !kinds || kinds.includes(e.kind))

  return (
    <div className="inbox-page">
      <div className="inbox-head">
        <h1>Inbox</h1>
        <span className="sub">{waiting ? `${waiting} waiting on you` : 'All caught up'}</span>
      </div>
      <div className="tabs">
        <button className={tab === 'waiting' ? 'on' : ''} onClick={() => setTab('waiting')}>Waiting · {waiting}</button>
        <button className={tab === 'history' ? 'on' : ''} onClick={() => setTab('history')}>History</button>
      </div>
      {list.isError && <div className="error">Couldn't load the Inbox.</div>}

      {tab === 'history' ? (
        <div className="stack fade-in" style={{ gap: 12 }}>
          <input className="input inbox-search" placeholder="Search past decisions" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search past decisions" />
          <div className="hist-list">
            {list.data?.map((e) => {
              const [o, cls] = outcome(e)
              const n = note(e)
              return (
                <div key={e.kind + e.id} className="hist-item">
                  <div className="stack" style={{ gap: 3 }}><span className="w">{when(e.at)}</span><span className="k">{KIND[e.kind]}</span></div>
                  <div className="stack" style={{ gap: 4, minWidth: 0 }}>
                    <span className="t">{text(e)}</span>
                    {n && <span className="c"><span className="sub">Your comment · </span>{n}</span>}
                    {where(e)}
                  </div>
                  <span className={`pill ${cls}`}>{o}</span>
                </div>
              )
            })}
            {list.isSuccess && !list.data.length && <div className="hist-empty">{q ? `No past decisions match “${q}”.` : 'Nothing decided yet.'}</div>}
          </div>
        </div>
      ) : list.isSuccess && !list.data.length ? (
        <div className="empty-big">
          <div className="empty-title">Nothing waiting on you</div>
          <div className="sub">Questions, permissions, tasks and suggestions from the assistant land here. Answers you give offline are queued and sent when you reconnect.</div>
        </div>
      ) : (
        <div className="stack fade-in" style={{ gap: 18 }}>
          <div className="pills">
            {FILTERS.map(([label], i) => <button key={label} className={`filter ${filter === i ? 'on' : ''}`} onClick={() => setFilter(i)}>{label}</button>)}
          </div>
          {GROUPS.map(([title, ks, all]) => {
            const g = items.filter((e) => ks.includes(e.kind))
            if (!g.length) return null
            return (
              <div key={title} className="stack" style={{ gap: 8 }}>
                <div className="inbox-group">
                  <span>{title}</span>
                  {all && g.length > 1 && <button className="link-btn accent" disabled={!online || acceptAll.isPending} onClick={() => acceptAll.mutate()}>Accept all</button>}
                </div>
                {g.map((e) => (
                  <CardPlace.Provider key={e.kind + e.id} value={{ mode: 'inbox', where: where(e), when: ago(e.at) }}>
                    <InboxCard e={e} />
                  </CardPlace.Provider>
                ))}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
