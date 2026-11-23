import { useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useFriendSettings, useHubServices, useInvitePreview, useJoinHub, useLeaveHub, useMembership, useMembershipCheck, useMeta, usePatchHubService, useRecoverMembership,
  useRotateMemberKey, useSetAccelerator, useSetFriendSettings, useSetHubPolicy,
} from '../api/queries'
import { ago, stamp } from '../components/format'
import { Row, Saved, Seg, Switch } from '../components/settings'

// Settings → Hub (H1): join a hub for two internet hostnames, see the tunnel and key state, leave.

export function hubSummary(m: S['Membership']): string {
  return m.state === 'joined' ? m.address ?? 'Joined' : m.state === 'left' ? 'Left' : 'Not joined'
}

const TUNNEL_DOT: Record<S['TunnelStatus']['state'], string> = { connected: 'ok', connecting: 'warn', idle: 'bad', offline: 'bad', refused: 'bad', unknown: 'bad' }

/** The tunnel line's text: the state, and which way the traffic goes. */
export function tunnelText(t: NonNullable<S['Membership']['tunnel']>): string {
  const a = t.accelerator?.state
  if (t.state === 'connected' && a === 'active') return 'Connected · via cloudflared'
  if (a === 'starting' || a === 'ready') return `${t.state} · cloudflared starting…`
  return t.state
}

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

/** The join screen's preview: the typed name in place of the hub's example name, where it stands as a whole
 *  part of a label (capabilities.md). Display only; the hostnames are the ones the hub returns on joining. */
export function previewHostnames(ex: { instance: string; files: string } | null | undefined, exName: string | null | undefined, name: string) {
  if (!ex || !exName) return null
  const re = () => new RegExp(`(^|[.-])${exName.replace(/[^a-z0-9-]/g, '')}(?=$|[.:-])`, 'g')
  if (!re().test(ex.instance) || !re().test(ex.files)) return null
  const put = (h: string) => h.replace(re(), (_m, pre: string) => pre + name)
  return { instance: put(ex.instance), files: put(ex.files) }
}

function Join({ m, onJoined }: { m: S['Membership']; onJoined: (key: string) => void }) {
  const preview = useInvitePreview()
  const join = useJoinHub()
  const [invite, setInvite] = useState('')
  const [name, setName] = useState('')
  const [locked, setLocked] = useState(false)
  const [backup, setBackup] = useState<'made' | 'skipped' | null>(null)
  const [svcOn, setSvcOn] = useState<Record<string, boolean>>({})
  const p = preview.data
  const nameOk = name.length >= 3 && name.length <= 32 && NAME_RE.test(name)
  const yours = nameOk ? previewHostnames(p?.example, p?.example_name, name) : null
  const blocked = !!p?.problems?.length
  const ready = !!p && !blocked && nameOk && !!backup && !join.isPending
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
          preview.mutate(invite.trim(), { onSuccess: (r) => { if (r.name_hint) setName(r.name_hint); setSvcOn(Object.fromEntries((r.services ?? []).map((x) => [x.address, x.default_on]))); setLocked(!!r.name_bound && !!r.name_hint) } })
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
            const services_off = (p?.services ?? []).filter((x) => !(svcOn[x.address] ?? x.default_on)).map((x) => x.address)
            join.mutateAsync({ invite: invite.trim(), name, backup, services_off })
              .then((r) => { onJoined(r.recovery_key); setInvite(''); preview.reset() }, () => {})
          }}>
            <label className="stack" style={{ gap: 4 }}>
              <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Name</span>
              <input className="input" autoComplete="off" spellCheck={false} maxLength={32} value={name} readOnly={locked} onChange={(e) => setName(e.target.value.toLowerCase())} />
              <span className="sub" style={{ fontSize: 12.5 }}>{locked ? 'This invite is for this name only.' : '3–32 lowercase letters, digits and single dashes.'} A name can never be reused, even after you leave.</span>
            </label>
            {yours && (
              <div className="stack" style={{ gap: 2 }} aria-label="Your hostnames">
                <span className="k" style={{ fontWeight: 500, fontSize: 13.5 }}>Your hostnames</span>
                <span className="mono" style={{ fontSize: 13 }}>{yours.instance}</span>
                <span className="mono" style={{ fontSize: 13 }}>{yours.files}</span>
                <span className="sub" style={{ fontSize: 12.5 }}>A preview: the hub confirms them when you join.</span>
              </div>
            )}
            {!!p.services?.length && (
              <div className="stack" style={{ gap: 6 }} aria-label="Services">
                <span style={{ fontWeight: 500, fontSize: 13.5 }}>Services</span>
                <span className="sub" style={{ fontSize: 12.5 }}>What {p.hub.name} offers. You can change these any time in Settings → Hub.</span>
                <div className="svc-list">
                  {p.services.map((x) => (
                    <div key={x.address} className="svc" data-testid={`join-svc-${x.address}`}>
                      <div className="set-row">
                        <div className="stack" style={{ gap: 3, minWidth: 0 }}>
                          <span style={{ fontWeight: 500, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>{x.title}<span className="pill mute">{x.kind}</span></span>
                          {x.description && <span className="sub">{x.description}</span>}
                          {x.extension && <span className="sub">Installs an extension.</span>}
                        </div>
                        <Switch on={svcOn[x.address] ?? x.default_on} label={`${x.title} on`} onChange={(on) => setSvcOn((s) => ({ ...s, [x.address]: on }))} />
                      </div>
                    </div>
                  ))}
                </div>
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

const REQUESTS_FROM: [S['FriendSettings']['requests_from'], string][] = [['anyone', 'Anyone'], ['hub', 'Members of my hub'], ['codes', 'Only with a friend code']]

/** Settings → Hub → Friends: who may ask, and the pause switch. Both save as you change them. */
function FriendSettings() {
  const q = useFriendSettings()
  const set = useSetFriendSettings()
  const [savedAt, setSavedAt] = useState(0)
  const d = q.data
  const pending = set.isPending ? set.variables : undefined
  const save = (b: S['FriendSettingsPatch']) => set.mutate(b, { onSuccess: () => setSavedAt((n) => n + 1) })
  return (
    <section className="set-section">
      <div className="set-head"><div className="set-title">Friends</div><Saved token={savedAt} error={set.error ? problem(set.error) : null} /></div>
      {q.error && <div className="error" role="alert">{problem(q.error)}</div>}
      {d && (
        <div className="set-rows one">
          <Row label="Who can send you friend requests" hint="Requests wait for your approval: nobody becomes a friend until you say so.">
            <Seg value={pending?.requests_from ?? d.requests_from} options={REQUESTS_FROM} onChange={(v) => save({ requests_from: v })} label="Who can send you friend requests" />
          </Row>
          <Row label="Pause all friend activity" hint="Nothing goes out, and what comes in is refused so senders try again later. Your friends and their settings stay as they are.">
            <Switch on={pending?.paused ?? d.paused} onChange={(v) => save({ paused: v })} label="Pause all friend activity" />
          </Row>
        </div>
      )}
      <div className="sub" style={{ fontSize: 13 }}><Link to="/friends">Open Friends</Link></div>
    </section>
  )
}

const SERVICE_DOT: Record<S['HubService']['state'], string> = { connected: 'ok', connecting: 'warn', error: 'bad', off: 'mute' }
const EXT_PILL: Record<NonNullable<S['HubService']['extension_state']>, [string, string]> = {
  installed: ['ok', 'Installed'], installing: ['warn', 'Installing…'], waiting: ['warn', 'Newer version waiting for you'], error: ['bad', 'Not installed'], off: ['mute', 'Off'],
}

/** Settings → Hub → Services: what the hub offers this instance, each with its own switch, and its extension (installed by the ops
 *  container at the commit the hub's signed document names) with another. Every switch saves as you change it. */
export function HubServices({ m }: { m: S['Membership'] }) {
  const q = useHubServices()
  const patch = usePatchHubService()
  const policy = useSetHubPolicy()
  const ops = !!useMeta().data?.capabilities.ops
  const [savedAt, setSavedAt] = useState(0)
  const done = { onSuccess: () => setSavedAt((n) => n + 1) }
  const list = q.data ?? []
  const withExt = list.some((x) => x.extension)
  if (!q.data && !q.error) return null
  return (
    <section className="set-section">
      <div className="set-head"><div className="set-title">Services</div><Saved token={savedAt} error={patch.error || policy.error ? problem(patch.error ?? policy.error) : null} /></div>
      {q.error && <div className="error" role="alert">{problem(q.error)}</div>}
      {!list.length && !q.error && <div className="empty">{m.hub?.name ?? 'Your hub'} offers no services.</div>}
      <div className="svc-list">
        {list.map((x) => {
          const pend = patch.isPending && patch.variables?.address === x.address ? patch.variables.body : undefined
          const on = pend?.enabled ?? x.enabled
          const extOn = pend?.extension_enabled ?? x.extension_enabled ?? true
          return (
            <div key={x.address} className="svc" data-testid={`svc-${x.address}`}>
              <div className="set-row">
                <div className="stack" style={{ gap: 3, minWidth: 0 }}>
                  <span style={{ fontWeight: 500, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span className={`dot ${SERVICE_DOT[x.state]}`} aria-hidden />
                    {x.title}
                    <span className="pill mute">{x.kind}</span>
                    <span className="sub">{x.state}</span>
                  </span>
                  {x.description && <span className="sub">{x.description}</span>}
                  <span className="sub">{x.address}{x.roles_held?.length ? ` · roles held: ${x.roles_held.join(', ')}` : ''}</span>
                  {x.error && <span className="sub" style={{ color: 'var(--badInk)' }}>{x.error}</span>}
                </div>
                <Switch on={on} label={`${x.title} on`} onChange={(enabled) => { if (!patch.isPending) patch.mutate({ address: x.address, body: { enabled } }, done) }} />
              </div>
              {x.extension && (
                <div className="set-row svc-ext">
                  <div className="stack" style={{ gap: 3, minWidth: 0 }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span className="k">Extension <code>{x.extension_name}</code></span>
                      {ops && x.extension_state && <span className={`pill ${EXT_PILL[x.extension_state][0]}`}>{EXT_PILL[x.extension_state][1]}</span>}
                      {x.extension_commit && <span className="sub mono" title={x.extension_commit}>{x.extension_commit.slice(0, 10)}</span>}
                    </span>
                    {!ops && <span className="sub">Installing it needs the ops container, which this install doesn't have.</span>}
                    {x.extension_state === 'waiting' && <span className="sub">The hub published <span className="mono">{x.extension.commit.slice(0, 10)}</span>.</span>}
                    {x.extension_error && <span className="sub" style={{ color: 'var(--badInk)' }}>{x.extension_error}</span>}
                  </div>
                  <div className="btns">
                    {x.extension_state === 'waiting' && (
                      <button className="btn small" disabled={patch.isPending} onClick={() => patch.mutate({ address: x.address, body: { extension_enabled: true } }, done)}>Update now</button>
                    )}
                    <Switch on={extOn} label={`${x.title} extension on`} onChange={(extension_enabled) => { if (ops && !patch.isPending) patch.mutate({ address: x.address, body: { extension_enabled } }, done) }} />
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
      {withExt && ops && (
        <div className="set-rows one">
          <Row label="Ask before a hub updates its extensions" hint="Off: when the hub publishes a newer version of an extension it installed, it is applied and logged. On: you get a card in your inbox and the old version stays until you agree.">
            <Switch on={policy.isPending ? !!policy.variables?.ask_before_extension_updates : !!m.ask_before_extension_updates} label="Ask before a hub updates its extensions"
              onChange={(ask_before_extension_updates) => policy.mutate({ ask_before_extension_updates }, done)} />
          </Row>
        </div>
      )}
    </section>
  )
}

function Joined({ m }: { m: S['Membership'] }) {
  const check = useMembershipCheck()
  const rotate = useRotateMemberKey()
  const recover = useRecoverMembership()
  const leave = useLeaveHub()
  const accel = useSetAccelerator()
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
          {m.accelerator?.offered && (
            <div className="set-row">
              <span>Use the hub's faster connection (cloudflared) when it offers one</span>
              <button className="toggle" role="switch" aria-checked={m.accelerator.enabled} aria-label="Use the hub's faster connection" disabled={accel.isPending}
                onClick={() => accel.mutate(!m.accelerator!.enabled)}>
                <span className={`switch ${m.accelerator.enabled ? 'on' : ''}`}><span /></span>
              </button>
            </div>
          )}
          {m.joined_at && <div className="set-row"><span className="k">Joined</span><span>{ago(m.joined_at)}</span></div>}
          {t && (
            <div className="set-row">
              <span className="k">Tunnel</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 8, textAlign: 'right' }}>
                <span className={`dot ${TUNNEL_DOT[t.state]}`} />{tunnelText(t)}{t.since ? ` · since ${ago(t.since)}` : ''}
              </span>
            </div>
          )}
        </div>
        {t?.error && <div className="error" role="alert">{t.error}</div>}
        {t?.accelerator?.state === 'error' && <div className="error" role="alert">cloudflared isn't working{t.accelerator.error ? `: ${t.accelerator.error}` : ''}. The normal tunnel is carrying your traffic.</div>}
        {accel.error && <div className="error" role="alert">{problem(accel.error)}</div>}
      </section>

      <HubServices m={m} />

      <FriendSettings />

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
