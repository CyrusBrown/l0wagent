import { useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import { useAcceptAll, useInbox, useInboxCounts } from '../api/queries'
import { InboxCard } from '../components/Cards'
import { ago } from '../components/format'
import { useOnline } from '../components/online'

type Kind = S['InboxEntry']['kind']
const FILTERS: [Kind | undefined, string][] = [[undefined, 'All'], ['question', 'Questions'], ['permission', 'Permissions'], ['task', 'Tasks'], ['suggestion', 'Suggestions'], ['promote', 'Promote'], ['security', 'Security']]

export function Inbox() {
  const [tab, setTab] = useState<'waiting' | 'history'>('waiting')
  const [kind, setKind] = useState<Kind | undefined>()
  const [q, setQ] = useState('')
  const counts = useInboxCounts()
  const list = useInbox(tab, kind, tab === 'history' ? q : undefined)
  const acceptAll = useAcceptAll()
  const online = useOnline()
  const openSuggestions = counts.data?.by_kind.suggestion ?? 0

  return (
    <div className="page narrow">
      <div className="page-head">
        <div className="stack" style={{ gap: 4 }}>
          <h1>Inbox</h1>
          <div className="sub">{counts.data?.waiting ? `${counts.data.waiting} waiting on you` : 'All caught up'}</div>
        </div>
        {tab === 'waiting' && openSuggestions > 1 && (
          <button className="btn" disabled={!online || acceptAll.isPending} onClick={() => acceptAll.mutate()}>Accept all suggestions · {openSuggestions}</button>
        )}
      </div>
      <div className="tabs">
        <button className={tab === 'waiting' ? 'on' : ''} onClick={() => setTab('waiting')}>Waiting · {counts.data?.waiting ?? 0}</button>
        <button className={tab === 'history' ? 'on' : ''} onClick={() => setTab('history')}>History</button>
      </div>
      <div className="row-between">
        <div className="pills">
          {FILTERS.map(([k, label]) => (
            <button key={label} className={`filter ${kind === k ? 'on' : ''}`} onClick={() => setKind(k)}>
              {label}{tab === 'waiting' && k && counts.data?.by_kind[k] ? ` · ${counts.data.by_kind[k]}` : ''}
            </button>
          ))}
        </div>
        {tab === 'history' && <input className="input" style={{ maxWidth: 240 }} placeholder="Search history" value={q} onChange={(e) => setQ(e.target.value)} />}
      </div>
      {list.isError && <div className="error">Couldn't load the Inbox.</div>}
      {list.isSuccess && list.data.length === 0 && (
        <div className="empty">{tab === 'waiting' ? 'Nothing waiting on you. Questions, permissions and suggestions from the assistant show up here.' : 'Nothing decided yet.'}</div>
      )}
      {list.data?.map((e) => (
        <div key={e.kind + e.id} className="stack" style={{ gap: 4 }}>
          <div className="row-between sub">
            {e.thread_id ? <Link to={`/t/${e.thread_id}`}>#{e.thread_title}</Link> : <span>{e.kind === 'security' ? 'Account' : ''}</span>}
            <span>{ago(e.at)}</span>
          </div>
          <InboxCard e={e} />
        </div>
      ))}
    </div>
  )
}
