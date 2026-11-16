import { type JSX, useState } from 'react'
import { NavLink, useParams } from 'react-router'
import type { S } from '../api/client'
import {
  problem, useChangePassword, useMe, useRevokeSession, useSelftest, useSessions, useSystem, useTotpDisable, useTotpEnable, useTotpSetup,
} from '../api/queries'
import { ago, clock, usageText } from '../components/format'
import { useTheme, type ThemePref } from '../app/theme'
import { Budgets, Instructions, Providers, Reviewer, Roles } from './SettingsBudget'
import { Notifications } from './SettingsNotify'

// Tab list and order from the mockup. Tabs without a screen yet say which milestone brings them.
const TABS: [slug: string, label: string, later?: string][] = [
  ['appearance', 'Appearance'],
  ['budgets', 'Budgets & levels'],
  ['providers', 'Providers'],
  ['roles', 'Roles'],
  ['reviewer', 'Reviewer'],
  ['instructions', 'Standing instructions'],
  ['account', 'Account'],
  ['notifications', 'Notifications'],
  ['extensions', 'Extensions', 'Extensions arrive with M8. They can add pages, sidebar sections, cards and settings panels here.'],
  ['system', 'System'],
]

export function Settings() {
  const tab = useParams().tab ?? 'budgets'  // the mockup opens Settings on Budgets & levels
  const cur = TABS.find(([slug]) => slug === tab) ?? TABS[0]
  return (
    <div className="settings">
      <nav className="settings-nav">
        <h1>Settings</h1>
        {TABS.map(([slug, label]) => <NavLink key={slug} to={`/settings/${slug}`} className={() => (slug === cur[0] ? 'on' : '')}>{label}</NavLink>)}
      </nav>
      <div className="settings-body">
        {PAGES[cur[0]] ?? <Later text={cur[2] ?? ''} title={cur[1]} />}
      </div>
    </div>
  )
}

const PAGES: Record<string, JSX.Element> = {
  appearance: <Appearance />, budgets: <Budgets />, providers: <Providers />, roles: <Roles />, reviewer: <Reviewer />, instructions: <Instructions />,
  account: <Account />, system: <System />, notifications: <Notifications />,
}

function Later({ title, text }: { title: string; text: string }) {
  return (
    <div className="placeholder">
      <div style={{ font: '500 18px var(--serif)' }}>{title}</div>
      <div className="sub" style={{ fontSize: 13.5, maxWidth: 420 }}>{text}</div>
    </div>
  )
}

function Appearance() {
  const { pref, setPref } = useTheme()
  const opts: [ThemePref, string, string][] = [['light', 'Light', 'Warm off-white'], ['dark', 'Dark', 'Warm charcoal'], ['system', 'Match system', 'Follows your OS setting']]
  return (
    <section className="set-section">
      <div className="set-title">Theme</div>
      <div className="theme-opts">
        {opts.map(([k, label, desc]) => (
          <button key={k} className={`theme-opt ${pref === k ? 'on' : ''}`} onClick={() => setPref(k)} aria-pressed={pref === k}>
            <span className="ring" />
            <span className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>{label}</span><span className="sub" style={{ fontSize: 12 }}>{desc}</span></span>
          </button>
        ))}
      </div>
    </section>
  )
}

// ── Account ──────────────────────────────────────────────────────────────
function Account() {
  const me = useMe()
  return <>
    <section className="set-section">
      <div className="set-title">Owner</div>
      <div className="rows">
        <div className="set-row"><span className="k">Username</span><span>{me.data?.username}</span></div>
        <div className="set-row"><span className="k">Name</span><span>{me.data?.display_name ?? '–'}</span></div>
        <div className="set-row"><span className="k">Timezone</span><span>{me.data?.timezone}</span></div>
      </div>
    </section>
    <Password />
    <TwoFactor enabled={!!me.data?.totp_enabled} />
    <section className="set-section">
      <div className="set-title">Passkeys</div>
      <div className="sub" style={{ fontSize: 13 }}>Sign in with a passkey instead of a password. Coming later; desktop app webviews support them unevenly.</div>
    </section>
    <Sessions />
  </>
}

function Password() {
  const change = useChangePassword()
  const [f, setF] = useState({ current: '', next: '', again: '' })
  const mismatch = f.again.length > 0 && f.next !== f.again
  return (
    <section className="set-section">
      <div className="set-title">Password</div>
      <form className="set-form" onSubmit={(e) => {
        e.preventDefault()
        change.mutate({ current_password: f.current, new_password: f.next }, { onSuccess: () => setF({ current: '', next: '', again: '' }) })
      }}>
        <input className="input" type="password" autoComplete="current-password" placeholder="Current password" value={f.current} onChange={(e) => setF({ ...f, current: e.target.value })} />
        <input className="input" type="password" autoComplete="new-password" placeholder="New password (12 characters or more)" value={f.next} onChange={(e) => setF({ ...f, next: e.target.value })} />
        <input className="input" type="password" autoComplete="new-password" placeholder="New password again" value={f.again} onChange={(e) => setF({ ...f, again: e.target.value })} />
        {mismatch && <div className="sub" style={{ color: 'var(--badInk)' }}>The new passwords don't match.</div>}
        {change.error && <div className="error" role="alert">{problem(change.error)}</div>}
        {change.isSuccess && <div className="done">Password changed. Your other sessions and devices were signed out.</div>}
        <div><button className="btn primary" disabled={!f.current || f.next.length < 12 || f.next !== f.again || change.isPending}>Change password</button></div>
      </form>
    </section>
  )
}

function TwoFactor({ enabled }: { enabled: boolean }) {
  const setup = useTotpSetup()
  const enable = useTotpEnable()
  const disable = useTotpDisable()
  const [code, setCode] = useState('')
  const [pw, setPw] = useState('')
  const [disabling, setDisabling] = useState(false)
  const pending = setup.data && !enabled
  return (
    <section className="set-section">
      <div className="set-title">Two-factor authentication</div>
      {enabled ? (
        <div className="stack">
          <div className="done">On. Sign-ins ask for a code from your authenticator app.</div>
          {disabling ? (
            <form className="set-form" onSubmit={(e) => { e.preventDefault(); disable.mutate({ password: pw }, { onSuccess: () => { setDisabling(false); setPw(''); setup.reset() } }) }}>
              <input className="input" type="password" autoComplete="current-password" placeholder="Your password" value={pw} onChange={(e) => setPw(e.target.value)} />
              {disable.error && <div className="error" role="alert">{problem(disable.error)}</div>}
              <div className="btns"><button className="btn danger" disabled={!pw || disable.isPending}>Turn off</button><button type="button" className="btn" onClick={() => setDisabling(false)}>Cancel</button></div>
            </form>
          ) : <div><button className="btn" onClick={() => setDisabling(true)}>Turn off…</button></div>}
        </div>
      ) : pending ? (
        <form className="totp" onSubmit={(e) => { e.preventDefault(); enable.mutate({ secret: setup.data!.secret, code }, { onSuccess: () => setCode('') }) }}>
          <img alt="QR code for your authenticator app" width={164} height={164} src={`data:image/svg+xml;base64,${btoa(setup.data!.qr_svg)}`} />
          <div className="stack">
            <div style={{ fontSize: 13.5 }}>Scan this with your authenticator app, or enter the key by hand:</div>
            <code style={{ wordBreak: 'break-all' }}>{setup.data!.secret}</code>
            <input className="input code-input" inputMode="numeric" autoComplete="one-time-code" placeholder="6-digit code" value={code} onChange={(e) => setCode(e.target.value)} />
            {enable.error && <div className="error" role="alert">{problem(enable.error)}</div>}
            <div className="btns"><button className="btn primary" disabled={code.trim().length < 6 || enable.isPending}>Turn on</button><button type="button" className="btn" onClick={() => setup.reset()}>Cancel</button></div>
          </div>
        </form>
      ) : (
        <div className="row-between" style={{ background: 'var(--side)', borderRadius: 12, padding: '14px 16px' }}>
          <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Off</span><span className="sub">Recommended before this server faces the internet (when you join a hub).</span></div>
          <button className="btn primary" disabled={setup.isPending} onClick={() => setup.mutate()}>Set up</button>
        </div>
      )}
    </section>
  )
}

function Sessions() {
  const sessions = useSessions()
  const revoke = useRevokeSession()
  return (
    <section className="set-section">
      <div className="set-title">Sessions and devices</div>
      <div className="rows">
        {sessions.data?.map((s) => (
          <div key={s.id} className="set-row">
            <div className="stack" style={{ gap: 2 }}>
              <span style={{ fontWeight: 500 }}>{s.label} {s.current && <span className="pill ok" style={{ marginLeft: 6 }}>This device</span>}</span>
              <span className="sub">{s.kind === 'device' ? 'App token' : 'Browser session'}{s.ip ? ` · ${s.ip}` : ''} · signed in {ago(s.created_at)} · last seen {ago(s.last_seen_at)}</span>
            </div>
            {!s.current && <button className="btn small danger" disabled={revoke.isPending} onClick={() => revoke.mutate(s.id)}>Revoke</button>}
          </div>
        ))}
      </div>
    </section>
  )
}

// ── System ───────────────────────────────────────────────────────────────
function System() {
  const sys = useSystem()
  const test = useSelftest()
  const d = sys.data
  return <>
    <section className="set-section">
      <div className="set-title">Status</div>
      {sys.error && <div className="error">{problem(sys.error)}</div>}
      {d && (
        <div className="rows">
          <div className="set-row"><span className="k">Server</span><span>l0wagent {d.server_version}</span></div>
          <div className="set-row"><span className="k">Agent computer</span><Health ok={d.runner.reachable} text={d.runner.reachable ? `${d.runner.busy ? 'running a session' : 'idle'} · ${d.runner.runtime_version ?? ''}` : d.runner.error ?? 'unreachable'} /></div>
          <div className="set-row"><span className="k">Claude login</span><Health ok={!!d.runner.logged_in} warn text={d.runner.logged_in ? 'logged in' : 'not logged in: docker compose exec -u agent agent claude auth login'} /></div>
          <div className="set-row"><span className="k">Egress proxy</span><Health ok={d.egress.reachable} text={d.egress.reachable ? (d.egress.lease_active ? 'model access open (a run holds the lease)' : 'model access closed') : d.egress.error ?? 'unreachable'} /></div>
          <div className="set-row"><span className="k">Usage</span><Health ok={d.usage?.seven_day_pct != null} warn text={d.usage?.seven_day_pct != null ? `${usageText(d.usage)} · ${d.usage.source} · ${clock(d.usage.fetched_at)}` : d.usage?.error ?? 'unknown'} /></div>
          <div className="set-row"><span className="k">Runs can start</span><Health ok={d.gate.open} warn text={d.gate.open ? 'yes' : d.gate.reason ?? 'no'} /></div>
        </div>
      )}
    </section>
    <section className="set-section">
      <div className="row-between">
        <div className="set-title">Self-test</div>
        <button className="btn" disabled={test.isPending} onClick={() => test.mutate()}>{test.isPending ? 'Running…' : test.data ? 'Run again' : 'Run self-test'}</button>
      </div>
      <div className="sub" style={{ fontSize: 13 }}>Checks that the agent's computer is isolated: no direct internet, no LAN or host, no owner API, no model access without a lease.</div>
      {test.error && <div className="error">{problem(test.error)}</div>}
      {test.data && <SelfTestList r={test.data} />}
    </section>
  </>
}

function Health({ ok, warn, text }: { ok: boolean; warn?: boolean; text: string }) {
  return <span style={{ display: 'flex', alignItems: 'center', gap: 8, textAlign: 'right' }}><span className={`dot ${ok ? 'ok' : warn ? 'warn' : 'bad'}`} />{text}</span>
}

const MARK: Record<S['SelfTestCheck']['result'], [string, string]> = { ok: ['✓', 'var(--okInk)'], warn: ['!', 'var(--warnInk)'], fail: ['✗', 'var(--badInk)'], skip: ['–', 'var(--faint)'] }

export function SelfTestList({ r }: { r: S['SelfTestResult'] }) {
  return (
    <div className="stack">
      <div className="rows">
        {r.checks.map((c) => (
          <div key={c.id} className="selftest-row">
            <span style={{ color: MARK[c.result][1], width: 14 }}>{MARK[c.result][0]}</span>
            <span style={{ flex: 1 }}>{c.label}</span>
            <span className="sub">{c.detail}</span>
          </div>
        ))}
      </div>
      <div className="sub">{r.note}</div>
    </div>
  )
}
