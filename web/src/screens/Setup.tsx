import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { problem, useAddApiProvider, useBudget, useClaudeLoginCode, useClaudeLoginStart, useLogin, usePatchBudget, useProviders, useSelftest, useSetupOwner, useSystem } from '../api/queries'
import { SelfTestList } from './Settings'

// The mockup's wizard. The Hub step appears once /api/meta advertises a hub (later).
const STEPS: [label: string, title: string, text: string][] = [
  ['Owner account', 'Create the owner', 'This is the only account on this server. You can add 2FA later in Settings.'],
  ['Timezone', 'Your timezone', 'Wakeups, protected hours and quiet hours use this.'],
  ['Provider', 'Connect a model provider', 'Sign the assistant in to a Claude subscription (the login runs on the server and stays on its computer), or give it an opencode Go key.'],
  ['Level', 'How much should it work?', 'A level sets the weekly budget and when it may wake. You can change it any time in Settings, or Boost it for a busy week.'],
  ['Self-test', 'Self-test', 'Checks that the assistant\'s computer is isolated the way it should be.'],
]
const LAST = STEPS.length - 1

const zones = (): string[] => {
  try {
    return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? []
  } catch {
    return []
  }
}

export function Setup({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [f, setF] = useState({ display_name: '', username: '', password: '', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' })
  const create = useSetupOwner()
  const login = useLogin()
  const created = create.isSuccess && login.isSuccess
  const err = create.error ?? login.error
  const tzList = useMemo(() => { const z = zones(); return z.includes(f.timezone) ? z : [f.timezone, ...z] }, [f.timezone])

  const finishAccount = () => {
    if (created) return setStep(2)
    create.mutate({ username: f.username.trim(), password: f.password, display_name: f.display_name.trim() || null, timezone: f.timezone }, {
      onSuccess: () => login.mutate({ username: f.username.trim(), password: f.password, want: 'session' }, { onSuccess: () => setStep(2) }),
    })
  }
  const canContinue = step === 0 ? f.username.trim().length > 0 && f.password.length >= 12 : step === 1 ? !!f.timezone && !create.isPending && !login.isPending : true
  const next = () => {
    if (step === 0) setStep(1)
    else if (step === 1) finishAccount()
    else if (step < LAST) setStep(step + 1)
    else { qc.invalidateQueries(); onDone() }
  }

  return (
    <div className="setup">
      <div className="setup-grid">
        <div className="setup-steps">
          <div className="setup-logo">l0wagent</div>
          {STEPS.map(([label], i) => (
            <button key={label} className={`setup-step ${i === step ? 'cur' : ''} ${i <= step ? 'reached' : ''}`} disabled={i > step || (created && i < 2)} onClick={() => setStep(i)}>
              <span className={`setup-dot ${i < step ? 'done' : i === step ? 'cur' : ''}`}>{i < step ? '✓' : i + 1}</span>{label}
            </button>
          ))}
        </div>
        <form className="setup-body" onSubmit={(e) => { e.preventDefault(); if (canContinue) next() }}>
          <h2>{STEPS[step][1]}</h2>
          <div className="setup-text">{STEPS[step][2]}</div>
          {step === 0 && (
            <div className="setup-fields">
              <input className="input big" placeholder="Your name" autoComplete="name" value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} />
              <input className="input big" placeholder="Username" autoComplete="username" autoFocus value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
              <input className="input big" type="password" placeholder="Password (12 characters or more)" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
            </div>
          )}
          {step === 1 && (
            <div className="setup-fields">
              <select className="input big" value={f.timezone} onChange={(e) => setF({ ...f, timezone: e.target.value })}>
                {tzList.map((z) => <option key={z} value={z}>{z}</option>)}
              </select>
              <div className="sub">Detected from this browser.</div>
            </div>
          )}
          {step === 2 && <ProviderStep />}
          {step === 3 && <LevelStep />}
          {step === 4 && <SelfTestStep />}
          {err && <div className="error" role="alert" style={{ maxWidth: 520 }}>{problem(err)}</div>}
          <div className="btns" style={{ paddingTop: 6 }}>
            {step > 0 && !(created && step === 2) && <button type="button" className="btn" onClick={() => setStep(step - 1)}>Back</button>}
            <button className="btn primary" disabled={!canContinue}>{step === LAST ? 'Open l0wagent' : step === 1 && (create.isPending || login.isPending) ? 'Creating…' : 'Continue'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ProviderStep() {
  const sys = useSystem()
  const r = sys.data?.runner
  return (
    <div className="setup-fields" style={{ maxWidth: 520 }}>
      <div className="provider-card">
        <div className="stack" style={{ gap: 2 }}>
          <span style={{ font: '500 14px var(--sans)' }}>Anthropic · Claude subscription</span>
          <span className="sub">{!r ? 'Checking…' : !r.reachable ? "The assistant's computer isn't reachable yet." : r.logged_in ? 'Connected · usage from provider' : 'Not logged in yet'}</span>
        </div>
        <span className={`pill ${r?.logged_in ? 'ok' : 'mute'}`}>{r?.logged_in ? '✓ Connected' : 'Not connected'}</span>
      </div>
      {r?.reachable && !r.logged_in && <ClaudeLogin />}
      <GoKey />
    </div>
  )
}

/** No Claude subscription: an opencode Go key, and every role moves onto it. */
function GoKey() {
  const providers = useProviders()
  const add = useAddApiProvider()
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState('')
  const saved = providers.data?.some((p) => p.kind === 'opencode-go')
  return (
    <div className="provider-card" style={{ flexWrap: 'wrap' }}>
      <div className="stack" style={{ gap: 2 }}>
        <span style={{ font: '500 14px var(--sans)' }}>opencode Go</span>
        <span className="sub">{saved ? 'Key saved · the assistant runs on opencode' : 'Open models through opencode, instead of Claude'}</span>
      </div>
      {saved ? <span className="pill ok">✓ Connected</span> : !open && <button type="button" className="btn small" onClick={() => setOpen(true)}>Use opencode Go</button>}
      {open && !saved && (
        <div className="stack" style={{ gap: 6, width: '100%', marginTop: 8 }}>
          <div className="inline-edit">
            <input className="input" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="opencode API key" aria-label="opencode Go key" style={{ maxWidth: 320 }} />
            <button type="button" className="btn primary" disabled={key.trim().length < 20 || add.isPending}
              onClick={() => add.mutate({ key: key.trim(), label: null, kind: 'opencode-go', use_for_all_roles: true }, { onSuccess: () => setKey('') })}>Save</button>
          </div>
          {add.error && <div className="error">{problem(add.error)}</div>}
          <div className="sub" style={{ fontSize: 12.5 }}>From opencode.ai → your workspace → API keys. Stored encrypted, write-only. Every role (wakeups, replies, the reviewer) moves to it; change that in Settings → Roles.</div>
        </div>
      )}
    </div>
  )
}

/** Sign the assistant in: the server runs `claude auth login`, you open its link, sign in with
 * the Claude account the assistant should use, and paste back the code Claude shows. */
export function ClaudeLogin() {
  const start = useClaudeLoginStart()
  const finish = useClaudeLoginCode()
  const [code, setCode] = useState('')
  if (finish.data?.ok) return <div className="done">{finish.data.message}</div>
  return (
    <div className="stack" style={{ gap: 8 }}>
      {!start.data ? (
        <div><button type="button" className="btn primary" disabled={start.isPending} onClick={() => start.mutate()}>{start.isPending ? 'Starting…' : 'Sign in to Claude'}</button></div>
      ) : (
        <>
          <div className="sub" style={{ fontSize: 13, lineHeight: 1.6 }}>
            1. <a href={start.data.url} target="_blank" rel="noreferrer">Open the Claude sign-in</a> and sign in with the account the assistant should use.<br />
            2. Paste the code it shows:
          </div>
          <div className="inline-edit">
            <input className="input" value={code} onChange={(e) => setCode(e.target.value)} placeholder="Code from Claude" aria-label="Code from Claude" style={{ maxWidth: 320 }} />
            <button type="button" className="btn primary" disabled={code.trim().length < 4 || finish.isPending} onClick={() => finish.mutate(code.trim())}>{finish.isPending ? 'Checking…' : 'Finish'}</button>
          </div>
          {finish.data && !finish.data.ok && <div className="error">{finish.data.message} <button type="button" className="link-btn" onClick={() => { finish.reset(); setCode(''); start.mutate() }}>Start again</button></div>}
        </>
      )}
      {start.error && <div className="error">{problem(start.error)}</div>}
      <div className="sub" style={{ fontSize: 12.5 }}>Or on the server: <code>./l0w claude-login</code>. You can continue now and sign in later (Settings → Providers).</div>
    </div>
  )
}

function LevelStep() {
  const budget = useBudget()
  const patch = usePatchBudget()
  const o = budget.data
  if (!o) return null
  const cur = o.providers[0]?.level
  return (
    <div className="stack" style={{ gap: 10, maxWidth: 620 }}>
      <div className="level-grid">
        {o.levels.map((l) => (
          <button type="button" key={l.name} className={`level-opt ${cur === l.name ? 'on' : ''}`} aria-pressed={cur === l.name} onClick={() => patch.mutate({ provider: o.providers[0]?.provider, level: l.name })}>
            <span style={{ font: '500 14px var(--sans)' }}>{l.name[0].toUpperCase() + l.name.slice(1)}</span>
            <span className="sub" style={{ fontSize: 12 }}>{l.limits.allowance ? `${l.limits.allowance} pts/wk` : 'Reply now only'}</span>
          </button>
        ))}
      </div>
      <div className="level-text">{o.levels.find((l) => l.name === cur)?.text}</div>
      <div className="sub" style={{ fontSize: 12.5 }}>
        {o.providers[0]?.kind === 'claude-subscription'
          ? `1 pt is 0.1% of your Claude subscription's week. Your own use is protected: no wakeup starts above ${o.reserve_weekly_pct}% of the week.`
          : `1 pt is $0.10 of ${o.providers[0]?.label ?? 'the provider'}'s usage.`}
      </div>
    </div>
  )
}

function SelfTestStep() {
  const test = useSelftest()
  const { mutate } = test
  useEffect(() => { mutate() }, [mutate])
  return (
    <div style={{ maxWidth: 520 }}>
      {test.isPending && <div className="sub">Running…</div>}
      {test.error && <div className="error">{problem(test.error)}</div>}
      {test.data && <SelfTestList r={test.data} />}
    </div>
  )
}
