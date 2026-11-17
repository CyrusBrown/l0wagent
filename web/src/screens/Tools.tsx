import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useAddIntegration, useConnectedApps, useDeleteIntegration, useDeleteSecret, useIntegrationPresets, useIntegrations, useProjects,
  useRevokeApp, useRoutines, useSecrets, useSetSecret, useSetToolPolicy, useSyncIntegration, useUpdateIntegration,
} from '../api/queries'
import { ago } from '../components/format'
import { useOnline } from '../components/online'
import { Switch } from '../components/settings'
import { Select } from '../components/Select'
import { Rules } from './Network'

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
      {tab === 'secrets' ? <Secrets /> : tab === 'network' ? <><Link className="link-btn accent" to="/network">Open the Network page for the full request log →</Link><Rules /></> : tab === 'integrations' ? <Integrations /> : <ConnectedApps />}
    </div>
  )
}

function Secrets() {
  const secrets = useSecrets()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const scopes = useScopeOptions()
  const rescope = useSetSecret()
  return (
    <div className="stack">
      <div className="row-between">
        <span className="sub" style={{ fontSize: 13, color: 'var(--muted)' }}>
          Values are write-only. The assistant gets them in its environment; they're redacted from everything shown here.
        </span>
        {!adding && <button className="btn primary small" onClick={() => setAdding(true)}>+ Add secret</button>}
      </div>
      {rescope.error && <div className="error">{problem(rescope.error)}</div>}
      {adding && <SecretForm onDone={() => setAdding(false)} />}
      <div className="rows">
        <div className="secrets-row head"><span>Name</span><span>Scope</span><span>Last read</span><span>Reads</span><span /></div>
        {secrets.data?.length === 0 && <div className="set-row sub">No secrets yet. Add one here, or the assistant asks for one with a secure-input card.</div>}
        {secrets.data?.map((x) => editing === x.name ? (
          <div key={x.name} style={{ padding: 12, borderBottom: '1px solid var(--soft)' }}><SecretForm existing={x} onDone={() => setEditing(null)} /></div>
        ) : (
          <div key={x.name} className="secrets-row">
            <div className="stack" style={{ gap: 2, minWidth: 0 }}><span className="mono" style={{ fontWeight: 500 }}>{x.name}</span><span className="sub">{x.description}</span></div>
            <span style={{ color: 'var(--fg2)', minWidth: 0 }}>
              <Select label={`Scope of ${x.name}`} value={x.scope} options={scopeOptions(scopes, x)} disabled={rescope.isPending}
                onChange={(scope) => rescope.mutate({ name: x.name, scope })} />
            </span>
            <span className="muted">{x.last_read_at ? ago(x.last_read_at) : 'never'}</span>
            <span className="muted">{x.reads}</span>
            <button className="link-btn accent" style={{ justifySelf: 'end' }} onClick={() => setEditing(x.name)}>Replace</button>
          </div>
        ))}
      </div>
    </div>
  )
}

const INTEGRATIONS_HINT = "Never in the agent's environment; only brokered tools use it"

function useScopeOptions() {
  const projects = useProjects()
  const routines = useRoutines()
  return [
    { value: 'agent', label: 'All projects' },
    ...(projects.data ?? []).map((p) => ({ value: `project:${p.slug}`, label: p.name, group: 'Projects' })),
    ...(routines.data ?? []).map((r) => ({ value: `routine:${r.id}`, label: r.name, group: 'Routines' })),
    { value: 'integrations', label: 'Integrations only', hint: INTEGRATIONS_HINT },
  ]
}

function scopeOptions(opts: { value: string; label: string; group?: string; hint?: string }[], x: S['SecretInfo']) {
  return opts.some((o) => o.value === x.scope) ? opts : [...opts, { value: x.scope, label: x.scope_label }]
}

function SecretForm({ existing, onDone }: { existing?: S['SecretInfo']; onDone: () => void }) {
  const set = useSetSecret()
  const del = useDeleteSecret()
  const online = useOnline()
  const scopes = useScopeOptions()
  const [f, setF] = useState({ name: existing?.name ?? '', description: existing?.description ?? '', value: '', scope: existing?.scope ?? 'agent' })
  return (
    <form className="card" onSubmit={(e) => { e.preventDefault(); set.mutate({ name: f.name.trim().toUpperCase(), value: f.value, description: f.description, scope: f.scope }, { onSuccess: onDone }) }}>
      <div className="card-label"><span>{existing ? `Replace ${existing.name}` : 'New secret'}</span><span className="r">stored as a secret, never sent to a model</span></div>
      {!existing && <input className="input mono" placeholder="NAME_LIKE_THIS" autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
      <input className="input" placeholder="What it's for (optional)" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
      <Select label="Scope" value={f.scope} options={existing ? scopeOptions(scopes, existing) : scopes} onChange={(scope) => setF({ ...f, scope })} />
      {f.scope === 'integrations' && <span className="sub">{INTEGRATIONS_HINT}</span>}
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

const POLICIES = [['allowed', 'Allowed'], ['ask', 'Ask'], ['blocked', 'Blocked']] as const

function Integrations() {
  const list = useIntegrations()
  const [adding, setAdding] = useState(false)
  return (
    <div className="stack">
      <div className="row-between">
        <span className="sub" style={{ fontSize: 13, color: 'var(--muted)' }}>
          Outside tool servers the assistant can use. Control calls them for it, so their keys never enter the agent's environment.
        </span>
        {!adding && <button className="btn primary small" onClick={() => setAdding(true)}>+ Add integration</button>}
      </div>
      {adding && <IntegrationForm onDone={() => setAdding(false)} />}
      {list.data?.length === 0 && !adding && <div className="set-row sub">No integrations yet. Add one to give the assistant tools such as GitHub.</div>}
      <div className="int-list">
        {list.data?.map((i) => <IntegrationCard key={i.name} i={i} />)}
      </div>
    </div>
  )
}

function IntegrationCard({ i }: { i: S['Integration'] }) {
  const online = useOnline()
  const sync = useSyncIntegration()
  const update = useUpdateIntegration()
  const del = useDeleteIntegration()
  const policy = useSetToolPolicy()
  const [confirm, setConfirm] = useState(false)
  const err = sync.error ?? update.error ?? del.error ?? policy.error
  const dot = i.status === 'ok' ? 'var(--okInk)' : i.status === 'error' ? 'var(--warnInk)' : 'var(--dim)'
  const statusText = i.status === 'ok' ? 'Connected' : i.status === 'error' ? `Error${i.last_error ? ` · ${i.last_error}` : ''}` : 'Not synced yet'
  return (
    <div className={`int-card ${i.enabled ? '' : 'off'}`}>
      <div className="int-head">
        <div className="int-title">
          <span className="int-dot" style={{ background: dot }} />
          <span className="int-name">{i.title}</span>
          <span className="int-status">{statusText}{i.synced_at && i.status === 'ok' ? ` · synced ${ago(i.synced_at)}` : ''}</span>
        </div>
        <div className="int-actions">
          <button className="link-btn accent" disabled={!online || sync.isPending} onClick={() => sync.mutate(i.name)}>{sync.isPending ? 'Syncing…' : 'Sync tools'}</button>
          <Switch label={`${i.title} enabled`} on={i.enabled} onChange={(enabled) => update.mutate({ name: i.name, enabled })} />
          {confirm ? (
            <span className="int-confirm">
              <button className="btn danger small" disabled={!online || del.isPending} onClick={() => del.mutate(i.name)}>Remove {i.title}</button>
              <button className="btn small" onClick={() => setConfirm(false)}>Keep</button>
            </span>
          ) : <button className="link-btn" onClick={() => setConfirm(true)}>Remove</button>}
        </div>
      </div>
      {!i.secret_saved && i.auth_secret && (
        <div className="int-note">
          <span className="mono">{i.auth_secret}</span> isn't saved yet.{' '}
          <Link className="link-btn accent" to="/tools">Save it on the Secrets tab</Link> with the scope "Integrations only".
        </div>
      )}
      {err && <div className="error" style={{ margin: '8px 14px' }}>{problem(err)}</div>}
      {i.tools.length === 0 && <div className="int-note">No tools yet. Sync to fetch them.</div>}
      {i.tools.map((t) => (
        <div key={t.name} className={`int-tool ${t.present ? '' : 'gone'}`} title={t.description ?? undefined}>
          <span className="mono int-tool-name">{t.name}</span>
          <span className={`pill ${t.read_only ? 'mute' : 'warn'}`}>{t.read_only ? 'Read-only' : 'Can write'}</span>
          {!t.present && <span className="sub">no longer offered</span>}
          {t.changed_since_allowed && <span className="pill warn" title="The server changed this tool's description or schema, so it went back to Ask.">Changed since you allowed it</span>}
          <div className="seg" role="radiogroup" aria-label={`Policy for ${t.name}`}>
            {POLICIES.map(([k, label]) => (
              <button key={k} type="button" role="radio" aria-checked={t.policy === k} className={t.policy === k ? 'on' : ''} disabled={!online}
                onClick={() => t.policy !== k && policy.mutate({ name: i.name, tool: t.name, policy: k })}>{label}</button>
            ))}
          </div>
        </div>
      ))}
      {i.recent_calls.length > 0 && (
        <div className="int-calls">
          <div className="card-label"><span>Recent calls</span></div>
          {i.recent_calls.slice(0, 5).map((c, n) => (
            <div key={n} className="int-call"><span className="mono">{c.tool}</span><span className="sub">{c.outcome}{c.detail ? ` · ${c.detail}` : ''}</span><span className="muted">{ago(c.at)}</span></div>
          ))}
        </div>
      )}
    </div>
  )
}

function IntegrationForm({ onDone }: { onDone: () => void }) {
  const presets = useIntegrationPresets()
  const add = useAddIntegration()
  const online = useOnline()
  const [choice, setChoice] = useState('custom')
  const [f, setF] = useState({ name: '', title: '', url: '', auth_secret: '', allow_private: false })
  const preset = presets.data?.find((p) => p.id === choice)
  const options = [...(presets.data ?? []).map((p) => ({ value: p.id, label: p.title })), { value: 'custom', label: 'Custom MCP server' }]
  const ready = preset ? true : !!(f.name.trim() && f.url.trim())
  return (
    <form className="card" onSubmit={(e) => {
      e.preventDefault()
      const body = preset ? { preset: preset.id } : { name: f.name.trim().toLowerCase(), title: f.title.trim() || null, url: f.url.trim(), auth_secret: f.auth_secret.trim().toUpperCase() || null, allow_private: f.allow_private }
      add.mutate(body, { onSuccess: onDone })
    }}>
      <div className="card-label"><span>New integration</span><span className="r">an MCP server over https</span></div>
      <Select label="Kind" value={choice} options={options} onChange={setChoice} />
      {preset ? (
        <div className="stack" style={{ gap: 8 }}>
          <span className="sub">{preset.guidance}</span>
          {preset.token_url && <a className="link-btn accent" href={preset.token_url} target="_blank" rel="noreferrer noopener">Create the token →</a>}
          <span className="sub">Uses the secret <span className="mono">{preset.auth_secret}</span>. Save it on the <Link className="link-btn accent" to="/tools">Secrets tab</Link> with the scope "Integrations only".</span>
        </div>
      ) : (
        <>
          <input className="input mono" placeholder="name-like-this" aria-label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          <input className="input" placeholder="Title (optional)" aria-label="Title" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
          <input className="input mono" placeholder="https://example.com/mcp" aria-label="URL" value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} />
          <input className="input mono" placeholder="SECRET_NAME for its key (optional)" aria-label="Secret name" value={f.auth_secret} onChange={(e) => setF({ ...f, auth_secret: e.target.value })} />
          <div className="set-row">
            <span className="sub">This server is on my own network (allows http and private addresses)</span>
            <Switch label="On my own network" on={f.allow_private} onChange={(allow_private) => setF({ ...f, allow_private })} />
          </div>
        </>
      )}
      {add.error && <div className="error">{problem(add.error)}</div>}
      <div className="btns">
        <button className="btn primary" disabled={!online || !ready || add.isPending}>Add</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
      </div>
    </form>
  )
}

const SCOPE_TEXT = { read: 'Read', write: 'Write', wake: 'Wake' } as const

function ConnectedApps() {
  const apps = useConnectedApps()
  const revoke = useRevokeApp()
  const online = useOnline()
  const [confirm, setConfirm] = useState<number | null>(null)
  return (
    <div className="stack">
      {revoke.error && <div className="error">{problem(revoke.error)}</div>}
      <div className="set-row sub">
        Connect at <span className="mono">{window.location.origin}/mcp</span>. To teach the outside model how to work with your assistant, add the{' '}
        <a className="link-btn accent" href="/connector/skill.zip" download>l0wagent skill</a>.
      </div>
      <div className="rows">
        {apps.data?.length === 0 && (
          <div className="set-row sub">
            Nothing is connected yet. Add l0wagent as a custom connector in Claude, ChatGPT or another MCP client.
          </div>
        )}
        {apps.data?.map((a) => (
          <div key={a.id} className="app-row">
            <div className="stack" style={{ gap: 2, minWidth: 0 }}>
              <span className="app-name">{a.name}</span>
              <span className="sub">{a.scopes.map((s) => SCOPE_TEXT[s]).join(' · ') || 'No access'} · {a.last_used_at ? `last used ${ago(a.last_used_at)}` : 'never used'}</span>
            </div>
            <div className="app-actions">
              {a.chat_id != null && <Link className="link-btn accent" to={`/c/${a.chat_id}`}>Open its chat</Link>}
              {confirm === a.id ? (
                <>
                  <button className="btn danger small" disabled={!online || revoke.isPending} onClick={() => revoke.mutate(a.id, { onSuccess: () => setConfirm(null) })}>Revoke {a.name}</button>
                  <button className="btn small" onClick={() => setConfirm(null)}>Keep</button>
                </>
              ) : <button className="btn danger small" onClick={() => setConfirm(a.id)}>Revoke</button>}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
