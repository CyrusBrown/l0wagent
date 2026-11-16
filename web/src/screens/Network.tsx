import { useState } from 'react'
import type { S } from '../api/client'
import { type NetFilter, problem, useAddEgressRule, useDeleteEgressRule, useNetwork } from '../api/queries'
import { useTimeFormat } from '../app/timeFormat'
import { Select } from '../components/Select'
import { bytes, clockSeconds, stamp } from '../components/format'
import { useOnline } from '../components/online'

const FILTERS: [NetFilter, string][] = [['all', 'All'], ['blocked', 'Blocked'], ['get', 'GET'], ['post', 'POST'], ['hour', 'Last hour']]
const RULE: Record<S['EgressRule']['action'], [string, string]> = { allow: ['Allow', 'ok'], allow_ro: ['Allow read-only', 'info'], block: ['Block', 'bad'] }

/** The mockup's NETWORK: egress's request log, the busiest hosts and the rules. */
export function Network() {
  useTimeFormat()
  const [filter, setFilter] = useState<NetFilter>('all')
  const net = useNetwork(filter)
  const d = net.data
  const max = Math.max(1, ...(d?.top_hosts.map((h) => h.requests) ?? []))
  return (
    <div className="page" style={{ maxWidth: 1136, padding: '28px 28px 40px', gap: 16 }}>
      <div className="row-between" style={{ flexWrap: 'wrap', gap: 12 }}>
        <h1>Network</h1>
        {d?.reachable && <span className="sub" style={{ fontSize: 13 }}>Last 24 hours · {d.requests_24h} requests · {d.blocked_24h} blocked</span>}
      </div>
      {d && !d.reachable && <div className="error" role="alert">Egress isn't answering, so the log and rules can't be read. The assistant can't reach the internet until it's back.</div>}
      <div className="net-grid">
        <div className="stack" style={{ gap: 10, minWidth: 0 }}>
          <div className="pills">
            {FILTERS.map(([k, l]) => <button key={k} className={`filter ${filter === k ? 'on' : ''}`} onClick={() => setFilter(k)}>{l}</button>)}
          </div>
          <div className="rows">
            <div className="net-row head"><span>Time</span><span>Method</span><span>Host</span><span>Status</span><span>Bytes</span></div>
            {d?.requests.length === 0 && <div className="set-row sub">{filter === 'all' ? 'No requests yet. Everything the assistant fetches from the internet is listed here.' : 'No requests match.'}</div>}
            {d?.requests.map((r) => (
              <div key={r.id} className={`net-row ${r.action === 'blocked' ? 'blocked' : ''}`} title={[r.url, r.reason].filter(Boolean).join(' · ') || undefined}>
                <span>{clockSeconds(r.at)}</span>
                <span>{r.method === 'CONNECT' ? 'TLS' : r.method}</span>
                <span className="ellipsis">{r.host}{r.port && ![80, 443].includes(r.port) ? `:${r.port}` : ''}{r.url && r.url.startsWith('/') ? r.url : ''}</span>
                <span>{r.action === 'blocked' ? 'blocked' : r.action === 'error' ? 'error' : r.status ?? 'ok'}</span>
                <span>{r.action === 'blocked' ? '0' : bytes(r.bytes_down)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="stack" style={{ gap: 20 }}>
          <div className="stack" style={{ gap: 8 }}>
            <div className="net-label">Top hosts</div>
            {d?.top_hosts.length === 0 && <span className="sub">Nothing in the last day.</span>}
            {d?.top_hosts.map((h) => (
              <div key={h.host} className="stack" style={{ gap: 4 }}>
                <div className="row-between mono" style={{ fontSize: 12.5 }}><span className="ellipsis">{h.host}</span><span style={{ color: 'var(--faint)' }}>{h.requests}</span></div>
                <div className="bar"><div style={{ width: `${Math.round((h.requests / max) * 100)}%` }} /></div>
              </div>
            ))}
          </div>
          <Rules compact />
        </div>
      </div>
    </div>
  )
}

/** Egress rules: the Network page's side list (compact) and the Tools & Secrets tab. */
export function Rules({ compact }: { compact?: boolean }) {
  useTimeFormat()
  const net = useNetwork()
  const del = useDeleteEgressRule()
  const online = useOnline()
  const [adding, setAdding] = useState(false)
  const d = net.data
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row-between" style={{ alignItems: 'baseline' }}>
        {compact ? <div className="net-label">Rules</div> : <span className="sub" style={{ fontSize: 13, color: 'var(--muted)' }}>What the assistant may reach on the internet. Rules you approve in permission cards show up here too.</span>}
        {!adding && <button className={compact ? 'link-btn' : 'btn primary small'} disabled={!online || !d?.reachable} onClick={() => setAdding(true)}>+ Add rule</button>}
      </div>
      {adding && <RuleForm onDone={() => setAdding(false)} />}
      {del.error && <div className="error" role="alert">{problem(del.error)}</div>}
      <div className="rows">
        {d?.rules.map((r) => (
          <div key={r.id} className={`rule-row ${compact ? 'compact' : ''}`}>
            <span className={`rule-kind ${RULE[r.action][1]}`}>{RULE[r.action][0]}</span>
            <span className="mono ellipsis">{r.pattern}</span>
            {!compact && <span className="rule-hits">{r.hits_7d} requests · 7 d</span>}
            <button className="icon-x" aria-label={`Remove the rule for ${r.pattern}`} disabled={!online || del.isPending} onClick={() => del.mutate(r.id)}>✕</button>
            {(r.note || r.created_at) && <span className="rule-note">{[r.note && (r.note.startsWith('permission #') ? `Added by approving ${r.note}` : r.note), r.created_at && stamp(r.created_at)].filter(Boolean).join(' · ')}</span>}
          </div>
        ))}
        {d?.reachable && (
          <div className={`rule-row ${compact ? 'compact' : ''}`}>
            <span className={`rule-kind ${d.default_policy === 'deny' ? 'bad' : 'ok'}`}>{d.default_policy === 'deny' ? 'Block' : 'Allow'}</span>
            <span className="mono">*</span>
            {!compact && <span />}
            <span />
            <span className="rule-note">Everything else{d.default_policy === 'deny' ? '' : ', except the local network and model providers outside a run'}</span>
          </div>
        )}
      </div>
    </div>
  )
}

function RuleForm({ onDone }: { onDone: () => void }) {
  const add = useAddEgressRule()
  const online = useOnline()
  const [f, setF] = useState<{ pattern: string; action: S['EgressRule']['action']; note: string }>({ pattern: '', action: 'allow', note: '' })
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); add.mutate(f, { onSuccess: onDone }) }}>
      <div className="card-label"><span>New rule</span><span className="r">applies to the assistant's next request</span></div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input className="input mono" style={{ flex: '1 1 200px' }} placeholder="api.example.com or *.example.com" autoFocus aria-label="Host or pattern"
          value={f.pattern} onChange={(e) => setF({ ...f, pattern: e.target.value })} />
        <Select variant="input" value={f.action} label="Rule" onChange={(v) => setF({ ...f, action: v as S['EgressRule']['action'] })}
          options={[{ value: 'allow', label: 'Allow' }, { value: 'allow_ro', label: 'Allow read-only' }, { value: 'block', label: 'Block' }]} />
      </div>
      <input className="input" placeholder="Why (optional)" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} />
      {add.error && <div className="error" role="alert">{problem(add.error)}</div>}
      <div className="btns">
        <button className="btn primary" disabled={!online || !f.pattern.trim() || add.isPending}>Add rule</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
      </div>
    </form>
  )
}
