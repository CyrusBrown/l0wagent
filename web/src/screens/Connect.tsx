import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { api, ApiError, ok, type S } from '../api/client'

type Scope = S['ConnectorScopeInfo']['scope']

const errorText = (e: unknown) => {
  const body = e instanceof ApiError ? (e.body as { error?: string; detail?: string } | undefined) : undefined
  const why = body?.detail || body?.error
  return `This connection request isn't valid${why ? `: ${why}` : '.'}`
}

export function Connect() {
  const [search] = useSearchParams()
  const params = Object.fromEntries(search.entries())
  const key = search.toString()
  const req = useQuery({
    queryKey: ['connector-request', key],
    retry: false,
    queryFn: () => ok(api.GET('/api/connector/request', { params: { query: params as never } })),
  })
  const [unchecked, setUnchecked] = useState<Scope[]>([])
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  const answer = async (approve: boolean) => {
    setBusy(true)
    setFailed(false)
    try {
      const scopes = (req.data?.scopes ?? []).map((s) => s.scope).filter((s) => !unchecked.includes(s))
      const r = await ok(api.POST('/api/connector/consent', { body: { params, approve, scopes } }))
      window.location.assign(r.redirect)
    } catch {
      setFailed(true)
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <div className="connect-card">
        <div className="login-head">
          <div className="login-title">l0wagent</div>
        </div>
        {req.isPending && <div className="sub" style={{ textAlign: 'center' }}>Checking the request…</div>}
        {req.error && <div className="error" role="alert">{errorText(req.error)}</div>}
        {req.data && <>
          <h1 className="connect-title">{req.data.client_name} wants to connect to your assistant</h1>
          <div className="sub">The app named itself, and control can't check that name. Only continue if you started this.</div>
          <div className="sub">You'll return to {req.data.redirect_host}.</div>
          <div className="connect-scopes">
            {req.data.scopes.map((s) => (
              <label key={s.scope} className="connect-scope">
                <input type="checkbox" aria-label={s.scope} checked={!unchecked.includes(s.scope)} disabled={s.scope === 'read' || busy}
                  onChange={(e) => setUnchecked(e.target.checked ? unchecked.filter((x) => x !== s.scope) : [...unchecked, s.scope])} />
                <span><b>{s.scope}</b><span className="sub">{s.description}</span></span>
              </label>
            ))}
          </div>
          <div className="sub">It can't see your secrets, approve anything or change settings. Cards it proposes wait for you in l0wagent.</div>
          {failed && <div className="error" role="alert">That didn't go through. Try again.</div>}
          <div className="connect-actions">
            <button className="btn" type="button" disabled={busy} onClick={() => void answer(false)}>Deny</button>
            <button className="btn primary" type="button" disabled={busy} onClick={() => void answer(true)}>Allow</button>
          </div>
        </>}
      </div>
    </div>
  )
}
