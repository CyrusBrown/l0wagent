import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { problem, useLogin, useSelftest, useSetupOwner, useSystem } from '../api/queries'
import { SelfTestList } from './Settings'

// The mockup's wizard, with the steps that exist so far. Level arrives with budgets (M4);
// the Hub step appears once /api/meta advertises a hub.
const STEPS: [label: string, title: string, text: string][] = [
  ['Owner account', 'Create the owner', 'This is the only account on this server. You can add 2FA later in Settings.'],
  ['Timezone', 'Your timezone', 'Wakeups, protected hours and quiet hours use this.'],
  ['Provider', 'Connect a model provider', 'The assistant uses its own Claude login, kept on its computer. Log it in from the server.'],
  ['Self-test', 'Self-test', 'Checks that the assistant\'s computer is isolated the way it should be.'],
]

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
    else if (step === 2) setStep(3)
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
          {step === 3 && <SelfTestStep />}
          {err && <div className="error" role="alert" style={{ maxWidth: 520 }}>{problem(err)}</div>}
          <div className="btns" style={{ paddingTop: 6 }}>
            {step > 0 && !(created && step === 2) && <button type="button" className="btn" onClick={() => setStep(step - 1)}>Back</button>}
            <button className="btn primary" disabled={!canContinue}>{step === 3 ? 'Open l0wagent' : step === 1 && (create.isPending || login.isPending) ? 'Creating…' : 'Continue'}</button>
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
      {r && !r.logged_in && (
        <div className="sub" style={{ fontSize: 13, lineHeight: 1.6 }}>
          On the server, run <code>docker compose exec -u agent agent claude auth login</code> and follow the link. Logging in from here comes with the installer. You can continue now and log in later.
        </div>
      )}
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
