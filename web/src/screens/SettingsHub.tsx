import { useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useInvitePreview, useJoinHub, useLeaveHub, useMembership, useMembershipCheck, useRecoverMembership, useRotateMemberKey,
} from '../api/queries'
import { ago, stamp } from '../components/format'

// Settings → Hub (H1): join a hub for two internet hostnames, see the tunnel and key state, leave.

export function hubSummary(m: S['Membership']): string {
  return m.state === 'joined' ? m.address ?? 'Joined' : m.state === 'left' ? 'Left' : 'Not joined'
}

const TUNNEL_DOT: Record<S['TunnelStatus']['state'], string> = { connected: 'ok', connecting: 'warn', idle: 'bad', offline: 'bad', refused: 'bad', unknown: 'bad' }

export function Hub() {
  const q = useMembership()
  // The recovery key lives only here: it isn't refetchable, so it survives only until dismissed.
  const [recovery, setRecovery] = useState<string | null>(null)
  const m = q.data
  if (q.error) return <div className="error" role="alert">{problem(q.error)}</div>
  if (!m) return null
  return (
    <div className="stack" style={{ gap: 24 }}>
      {recovery && <RecoveryKey value={recovery} onDone={() => setRecovery(null)} />}
      {!m.available ? (
        <section className="set-section">
          <div className="set-title">Hub</div>
          <div className="sub" style={{ fontSize: 13.5 }}>This install has no gateway service yet, so it can't join a hub. Run <code>./l0w update</code> on the server to add it.</div>
        </section>
      ) : m.state === 'joined' ? <Joined m={m} /> : <Join m={m} onJoined={setRecovery} />}
    </div>
  )
}

function RecoveryKey({ value, onDone }: { value: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false)
  return (
    <section className="set-section">
      <div className="set-title">Your recovery key</div>
      <div className="hub-panel">
        <div style={{ fontSize: 13.5 }}>
          Save this now. It is shown once and this instance doesn't keep it. You need it to recover your name if the instance's key is stolen.
        </div>
        <code className="hub-key" data-testid="recovery-key">{value}</code>
        <div className="btns">
          <button className="btn" onClick={() => { void navigator.clipboard?.writeText(value); setCopied(true) }}>{copied ? 'Copied' : 'Copy'}</button>
          <button className="btn primary" onClick={onDone}>I've saved it</button>
        </div>
      </div>
    </section>
  )
}

const NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/

function Join({ m, onJoined }: { m: S['Membership']; onJoined: (key: string) => void }) {
  const preview = useInvitePreview()
  const join = useJoinHub()
  const [invite, setInvite] = useState('')
  const [name, setName] = useState('')
  const [mode, setMode] = useState<'tunnel' | 'own-url'>('tunnel')
  const [urls, setUrls] = useState({ instance: '', files: '' })
  const [backup, setBackup] = useState<'made' | 'skipped' | null>(null)
  const p = preview.data
  const nameOk = name.length >= 3 && name.length <= 32 && NAME_RE.test(name)
  const urlsOk = mode === 'tunnel' || (/^https:\/\/\S+$/.test(urls.instance) && /^https:\/\/\S+$/.test(urls.files))
  const blocked = !!p?.problems?.length
  const ready = !!p && !blocked && nameOk && urlsOk && !!backup && !join.isPending
  return (
    <>
      <section className="set-section">
        <div className="set-title">Hub</div>
        {m.state === 'left' && <div className="sub" style={{ fontSize: 13.5 }}>You left {m.hub?.name ?? 'the hub'}{m.left_at ? ` ${ago(m.left_at)}` : ''}{m.address ? `; ${m.address} can never be used again` : ''}. You can join another hub, or this one with a new name.</div>}
        <div className="sub" style={{ fontSize: 13.5 }}>
          Joining a hub gives this instance two internet hostnames, one for the app and one for files, reachable from anywhere. The hub's operator can see
          your traffic's metadata, so join only a hub you trust.
        </div>
        <form className="set-form" style={{ maxWidth: 520 }} onSubmit={(e) => {
          e.preventDefault()
          preview.mutate(invite.trim(), { onSuccess: (r) => { if (r.name_hint) setName(r.name_hint); if (!r.own_url_allowed) setMode('tunnel') } })
        }}>
          <input className="input mono" autoComplete="off" spellCheck={false} placeholder="l0w-invite:…" aria-label="Invite" value={invite} onChange={(e) => setInvite(e.target.value)} />
          {preview.error && <div className="error" role="alert">{problem(preview.error)}</div>}
          <div><button className="btn" disabled={!invite.trim() || preview.isPending}>{preview.isPending ? 'Reading…' : 'Read invite'}</button></div>
        </form>
      </section>

      {p && (
        <section className="set-section hub-sub">
          <div className="set-title">{p.hub.name}</div>
          <div className="rows">
            <div className="set-row"><span className="k">Domain</span><span>{p.hub.domain}</span></div>
            {p.expires && <div className="set-row"><span className="k">Invite expires</span><span>{stamp(p.expires)}</span></div>}
            {p.example && <div className="set-row"><span className="k">Example hostnames</span><span className="mono" style={{ textAlign: 'right' }}>{p.example.instance}<br />{p.example.files}</span></div>}
          </div>
          {!!Object.keys(p.hub.policies ?? {}).length && (
            <div className="hub-links"><span className="sub">Policies:</span>{Object.entries(p.hub.policies ?? {}).map(([k, href]) => <a key={k} href={href} target="_blank" rel="noreferrer">{k}</a>)}</div>
          )}
          {p.problems?.map((x) => <div key={x} className="error" role="alert">{x}</div>)}
          {!p.two_factor_ready && <div className="sub" style={{ fontSize: 13 }}>Two-step sign-in isn't on. Turn it on in <Link to="/settings/account">Account settings</Link> first.</div>}

          <form className="set-form" style={{ maxWidth: 520, marginTop: 6 }} onSubmit={(e) => {
            e.preventDefault()
            if (!ready || !backup) return
            // mutateAsync, not mutate's per-call onSuccess: the membership refetch swaps this form for the joined
            // view before the answer arrives, and React Query drops per-call callbacks of an unmounted caller,
            // which would lose the recovery key (shown once, never refetchable). The parent keeps it.
            join.mutateAsync({ invite: invite.trim(), name, mode, backup, ...(mode === 'own-url' ? { own_urls: urls } : {}) })
              .then((r) => { onJoined(r.recovery_key); setInvite(''); preview.reset() }, () => {})
          }}>
            <label className="stack" style={{ gap: 4 }}>
              <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Name</span>
              <input className="input" autoComplete="off" spellCheck={false} maxLength={32} value={name} onChange={(e) => setName(e.target.value.toLowerCase())} />
              <span className="sub" style={{ fontSize: 12.5 }}>3–32 lowercase letters, digits and single dashes. A name can never be reused, even after you leave.</span>
            </label>
            {p.own_url_allowed && (
              <div className="stack" style={{ gap: 6 }}>
                <span style={{ fontWeight: 500, fontSize: 13.5 }}>How it's reached</span>
                <label className="hub-opt"><input type="radio" name="hub-mode" checked={mode === 'tunnel'} onChange={() => setMode('tunnel')} /><span>Through the hub's tunnel (default)</span></label>
                <label className="hub-opt"><input type="radio" name="hub-mode" checked={mode === 'own-url'} onChange={() => setMode('own-url')} /><span>My own URLs</span></label>
                {mode === 'own-url' && (
                  <div className="hub-sub">
                    <input className="input" placeholder="https:// app address" aria-label="App URL" value={urls.instance} onChange={(e) => setUrls({ ...urls, instance: e.target.value.trim() })} />
                    <input className="input" placeholder="https:// files address (a different origin)" aria-label="Files URL" value={urls.files} onChange={(e) => setUrls({ ...urls, files: e.target.value.trim() })} />
                  </div>
                )}
              </div>
            )}
            <div className="stack" style={{ gap: 6 }}>
              <span style={{ fontWeight: 500, fontSize: 13.5 }}>Backup</span>
              <label className="hub-opt"><input type="radio" name="hub-backup" checked={backup === 'made'} onChange={() => setBackup('made')} /><span>I've made a backup (<code>./l0w backup</code>)</span></label>
              <label className="hub-opt"><input type="radio" name="hub-backup" checked={backup === 'skipped'} onChange={() => setBackup('skipped')} /><span>Skip the backup</span></label>
            </div>
            {join.error && <div className="error" role="alert">{problem(join.error)}</div>}
            {join.isPending && <div className="sub" role="status">Joining. This can take about 20 seconds…</div>}
            <div><button className="btn primary" disabled={!ready}>{join.isPending ? 'Joining…' : 'Join'}</button></div>
          </form>
        </section>
      )}
    </>
  )
}

function Joined({ m }: { m: S['Membership'] }) {
  const check = useMembershipCheck()
  const rotate = useRotateMemberKey()
  const recover = useRecoverMembership()
  const leave = useLeaveHub()
  const [confirmRotate, setConfirmRotate] = useState(false)
  const [recovering, setRecovering] = useState(false)
  const [rkey, setRkey] = useState('')
  const [leaving, setLeaving] = useState(false)
  const [typed, setTyped] = useState('')
  const t = m.tunnel
  const c = m.check
  return (
    <>
      {m.hub_key_alarm && (
        <div className="hub-warn" role="alert"><strong>The hub's signing key changed.</strong> {m.hub_key_alarm}</div>
      )}
      <section className="set-section">
        <div className="set-title">Hub</div>
        <div className="rows">
          <div className="set-row"><span className="k">Address</span><span>{m.address}</span></div>
          {m.hub && <div className="set-row"><span className="k">Hub</span><span>{m.hub.name} · {m.hub.domain}</span></div>}
          {m.urls && <div className="set-row"><span className="k">App</span><a href={m.urls.instance} target="_blank" rel="noreferrer">{m.urls.instance}</a></div>}
          {m.urls && <div className="set-row"><span className="k">Files</span><a href={m.urls.files} target="_blank" rel="noreferrer">{m.urls.files}</a></div>}
          <div className="set-row"><span className="k">Mode</span><span>{m.mode === 'own-url' ? 'Own URLs' : 'Hub tunnel'}</span></div>
          {m.joined_at && <div className="set-row"><span className="k">Joined</span><span>{ago(m.joined_at)}</span></div>}
          {t && (
            <div className="set-row">
              <span className="k">Tunnel</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 8, textAlign: 'right' }}>
                <span className={`dot ${TUNNEL_DOT[t.state]}`} />{t.state}{t.since ? ` · since ${ago(t.since)}` : ''}
              </span>
            </div>
          )}
        </div>
        {t?.error && <div className="error" role="alert">{t.error}</div>}
      </section>

      <section className="set-section">
        <div className="row-between">
          <div className="set-title">Self-check</div>
          <button className="btn" disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : 'Check now'}</button>
        </div>
        {c ? (
          <>
            <div className="sub" style={{ fontSize: 13 }}>Last checked {ago(c.at)}: {c.ok ? <span className="pill ok">OK</span> : <span className="pill bad">Problems</span>}</div>
            {!!c.problems?.length && <div className="rows">{c.problems.map((x) => <div key={x} className="set-row">{x}</div>)}</div>}
          </>
        ) : <div className="sub" style={{ fontSize: 13 }}>Not checked yet.</div>}
        {check.error && <div className="error" role="alert">{problem(check.error)}</div>}
      </section>

      <section className="set-section">
        <div className="set-title">Signing key</div>
        {m.sign_key && <div className="sub" style={{ fontSize: 13 }}>Key <code>{m.sign_key.kid.slice(0, 12)}</code> · created {ago(m.sign_key.created)}</div>}
        {confirmRotate ? (
          <div className="hub-sub">
            <div className="sub" style={{ fontSize: 13 }}>Make a new signing key and register it with the hub? The old one stops working.</div>
            <div className="btns">
              <button className="btn primary" disabled={rotate.isPending} onClick={() => rotate.mutate(undefined, { onSuccess: () => setConfirmRotate(false) })}>{rotate.isPending ? 'Rotating…' : 'Rotate key'}</button>
              <button className="btn" onClick={() => setConfirmRotate(false)}>Cancel</button>
            </div>
          </div>
        ) : <div><button className="btn" onClick={() => setConfirmRotate(true)}>Rotate key…</button></div>}
        {rotate.error && <div className="error" role="alert">{problem(rotate.error)}</div>}

        <div><button className="link-btn" aria-expanded={recovering} onClick={() => setRecovering(!recovering)}>Recover with recovery key</button></div>
        {recovering && (
          <form className="set-form hub-sub" onSubmit={(e) => { e.preventDefault(); recover.mutate(rkey.trim(), { onSuccess: () => { setRkey(''); setRecovering(false) } }) }}>
            <div className="sub" style={{ fontSize: 13 }}>If this instance's key was lost or stolen, paste the recovery key you saved when you joined to register a new one.</div>
            <input className="input mono" autoComplete="off" spellCheck={false} placeholder="Recovery key" aria-label="Recovery key" value={rkey} onChange={(e) => setRkey(e.target.value)} />
            {recover.error && <div className="error" role="alert">{problem(recover.error)}</div>}
            <div><button className="btn" disabled={!rkey.trim() || recover.isPending}>{recover.isPending ? 'Recovering…' : 'Recover'}</button></div>
          </form>
        )}
      </section>

      <section className="set-section hub-danger">
        <div className="set-title">Leave hub</div>
        <div className="sub" style={{ fontSize: 13 }}>Your hostnames stop working. The name <strong>{m.address}</strong> can never be used again, by you or anyone else.</div>
        {leaving ? (
          <form className="set-form hub-sub" onSubmit={(e) => { e.preventDefault(); leave.mutate(typed, { onSuccess: () => { setLeaving(false); setTyped('') } }) }}>
            <input className="input" autoComplete="off" spellCheck={false} placeholder={`Type ${m.address} to confirm`} aria-label="Type the address to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} />
            {leave.error && <div className="error" role="alert">{problem(leave.error)}</div>}
            <div className="btns">
              <button className="btn danger" disabled={typed !== m.address || leave.isPending}>{leave.isPending ? 'Leaving…' : 'Leave hub'}</button>
              <button type="button" className="btn" onClick={() => { setLeaving(false); setTyped('') }}>Cancel</button>
            </div>
          </form>
        ) : <div><button className="btn danger" onClick={() => setLeaving(true)}>Leave hub…</button></div>}
      </section>
    </>
  )
}
