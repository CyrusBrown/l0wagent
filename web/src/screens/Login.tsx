import { useState } from 'react'
import { ApiError } from '../api/client'
import { useLogin, useTotp } from '../api/queries'

const message = (e: unknown) => {
  if (!(e instanceof ApiError)) return 'Something went wrong. Try again.'
  const body = e.body as { error?: string; retry_after_s?: number } | undefined
  switch (body?.error) {
    case 'invalid_credentials': return 'Wrong username or password.'
    case 'invalid_code': return "That code didn't work."
    case 'rate_limited': return `Too many attempts. Try again in ${Math.ceil((body.retry_after_s ?? 60) / 60)} min.`
    case 'locked': return `Locked for ${Math.ceil((body.retry_after_s ?? 900) / 60)} min after too many attempts. Your other devices were notified.`
    default: return `Sign-in failed (HTTP ${e.status}).`
  }
}

export function Login() {
  const login = useLogin()
  const totp = useTotp()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const challenge = login.data?.status === 'totp_required' ? login.data.challenge : null
  const err = challenge ? totp.error : login.error

  return (
    <div className="login">
      <form onSubmit={(e) => {
        e.preventDefault()
        if (challenge) totp.mutate({ challenge, code })
        else login.mutate({ username, password, want: 'session' })
      }}>
        <div className="login-head">
          <div className="login-title">l0wagent</div>
          <div className="sub" style={{ fontSize: 14, color: 'var(--muted)' }}>{challenge ? 'Enter the code from your authenticator app' : 'Sign in to your assistant'}</div>
        </div>
        {challenge ? <>
          <input className="input big code-input" aria-label="Code" autoFocus autoComplete="one-time-code" placeholder="6-digit or recovery code" value={code} onChange={(e) => setCode(e.target.value)} />
          {err && <div className="error" role="alert">{message(err)}</div>}
          <button className="btn primary big" type="submit" disabled={totp.isPending || code.trim().length < 6}>Sign in</button>
          <div className="sub" style={{ textAlign: 'center' }}>Your other devices will get a new-device notice.</div>
        </> : <>
          <input className="input big" aria-label="Username" placeholder="Username" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
          <input className="input big" aria-label="Password" placeholder="Password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          {err && <div className="error" role="alert">{message(err)}</div>}
          <button className="btn primary big" type="submit" disabled={login.isPending || !username || !password}>Continue</button>
        </>}
      </form>
    </div>
  )
}
