import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import { problem, useDeleteSecret, useSecrets, useSetSecret } from '../api/queries'
import { ago } from '../components/format'
import { useOnline } from '../components/online'

// Tabs from the mockup. Secrets are basic in M2; the rest of the page arrives with M6.
const TABS = [['secrets', 'Secrets'], ['integrations', 'Integrations'], ['network', 'Network rules'], ['apps', 'Connected apps']] as const

export function Tools() {
  const [params, setParams] = useSearchParams()
  const tab = TABS.find(([k]) => k === params.get('tab'))?.[0] ?? 'secrets'
  return (
    <div className="page" style={{ maxWidth: 1016, padding: "28px 28px 40px", gap: 18 }}>
      <h1>Tools &amp; Secrets</h1>
      <div className="tabs">
        {TABS.map(([k, label]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setParams(k === 'secrets' ? {} : { tab: k })}>{label}</button>)}
      </div>
      {tab === 'secrets' ? <Secrets /> : (
        <div className="placeholder">
          <div style={{ font: '500 18px var(--serif)' }}>{TABS.find(([k]) => k === tab)![1]}</div>
          <div className="sub" style={{ fontSize: 13.5, maxWidth: 440 }}>
            {tab === 'integrations' && 'Brokered MCP servers, per-tool policy and the GitHub preset arrive with M6.'}
            {tab === 'network' && <>Owner-managed egress rules get their own page in M6. Rules you approve in permission cards already apply; the full request log is on the <Link to="/network">Network</Link> page.</>}
            {tab === 'apps' && 'Outside chat services connected through the MCP connector arrive with M6.'}
          </div>
        </div>
      )}
    </div>
  )
}

function Secrets() {
  const secrets = useSecrets()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  return (
    <div className="stack">
      <div className="row-between">
        <span className="sub" style={{ fontSize: 13, color: 'var(--muted)' }}>
          Values are write-only. The assistant gets them in its environment; they're redacted from everything shown here.
        </span>
        {!adding && <button className="btn primary small" onClick={() => setAdding(true)}>+ Add secret</button>}
      </div>
      {adding && <SecretForm onDone={() => setAdding(false)} />}
      <div className="rows">
        <div className="secrets-row head"><span>Name</span><span>Scope</span><span>Last read</span><span>Reads</span><span /></div>
        {secrets.data?.length === 0 && <div className="set-row sub">No secrets yet. Add one here, or the assistant asks for one with a secure-input card.</div>}
        {secrets.data?.map((x) => editing === x.name ? (
          <div key={x.name} style={{ padding: 12, borderBottom: '1px solid var(--soft)' }}><SecretForm existing={x} onDone={() => setEditing(null)} /></div>
        ) : (
          <div key={x.name} className="secrets-row">
            <div className="stack" style={{ gap: 2, minWidth: 0 }}><span className="mono" style={{ fontWeight: 500 }}>{x.name}</span><span className="sub">{x.description}</span></div>
            <span style={{ color: 'var(--fg2)' }}>{x.scope === 'agent' ? 'Agent only' : x.scope}</span>
            <span className="muted">{x.last_read_at ? ago(x.last_read_at) : 'never'}</span>
            <span className="muted">{x.reads}</span>
            <button className="link-btn accent" style={{ justifySelf: 'end' }} onClick={() => setEditing(x.name)}>Replace</button>
          </div>
        ))}
      </div>
    </div>
  )
}

function SecretForm({ existing, onDone }: { existing?: S['SecretInfo']; onDone: () => void }) {
  const set = useSetSecret()
  const del = useDeleteSecret()
  const online = useOnline()
  const [f, setF] = useState({ name: existing?.name ?? '', description: existing?.description ?? '', value: '' })
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); set.mutate({ name: f.name.trim().toUpperCase(), value: f.value, description: f.description }, { onSuccess: onDone }) }}>
      <div className="card-label"><span>{existing ? `Replace ${existing.name}` : 'New secret'}</span><span className="r">stored as a secret, never sent to a model</span></div>
      {!existing && <input className="input mono" placeholder="NAME_LIKE_THIS" autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
      <input className="input" placeholder="What it's for (optional)" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
      <input className="input" type="password" autoComplete="off" placeholder="Paste the value" autoFocus={!!existing} value={f.value} onChange={(e) => setF({ ...f, value: e.target.value })} />
      {(set.error || del.error) && <div className="error">{problem(set.error ?? del.error)}</div>}
      <div className="btns">
        <button className="btn primary" disabled={!online || !f.value.trim() || !f.name.trim() || set.isPending}>Save</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
        {existing && <button type="button" className="btn danger" style={{ marginLeft: 'auto' }} disabled={!online || del.isPending}
          onClick={() => del.mutate(existing.name, { onSuccess: onDone })}>Delete</button>}
      </div>
    </form>
  )
}
