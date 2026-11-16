import { useEffect, useState } from 'react'
import type { S } from '../api/client'
import { problem, subscribePush, useNotificationSettings, usePushTest, useRemovePushSubscription, useSetNotificationSettings } from '../api/queries'
import { useTimeFormat } from '../app/timeFormat'
import { ago } from '../components/format'
import { TimePicker } from '../components/DateTimePicker'

// Settings → Notifications (M5), in the mockup's style: this device's push, which events
// notify (approvals and new sign-ins are on by default), quiet hours, and ntfy for installs
// without HTTPS.

function deviceLabel(): string {
  const ua = navigator.userAgent
  const os = /iPhone|iPad/.test(ua) ? 'iPhone/iPad' : /Android/.test(ua) ? 'Android' : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : 'Linux'
  const app = window.matchMedia('(display-mode: standalone)').matches ? 'installed app' : 'browser'
  return `${os} (${app})`
}

export function Notifications() {
  const { hour12 } = useTimeFormat()
  const q = useNotificationSettings()
  const save = useSetNotificationSettings()
  const test = usePushTest()
  const remove = useRemovePushSubscription()
  const [c, setC] = useState<S['NotificationSettings'] | null>(null)
  const [subErr, setSubErr] = useState<string | null>(null)
  const [subOk, setSubOk] = useState(false)
  useEffect(() => { if (q.data) setC(q.data) }, [q.data])
  if (!c) return null
  const put = (upd: Partial<S['NotificationSettings']>) => { const n = { ...c, ...upd }; setC(n); save.mutate(n) }
  const secure = window.isSecureContext
  return (
    <div className="stack" style={{ gap: 24 }}>
      <section className="set-section">
        <div className="set-title">This device</div>
        <div className="provider-card">
          <div className="stack" style={{ gap: 2 }}>
            <span style={{ font: '500 14px var(--sans)' }}>Push notifications</span>
            <span className="sub" style={{ fontSize: 12.5 }}>
              {secure ? 'Get notified here even when the app is closed.' : 'Needs HTTPS: open l0wagent at its https:// address (Tailscale HTTPS or a hub). Until then, ntfy below works.'}
            </span>
          </div>
          <button className="btn primary" disabled={!secure} onClick={() => subscribePush(deviceLabel()).then(() => { setSubOk(true); setSubErr(null); q.refetch() }, (e) => setSubErr(String(e.message ?? e)))}>
            Turn on here
          </button>
        </div>
        {subErr && <div className="error" role="alert">{subErr}</div>}
        {subOk && <div className="done">This device gets notifications now.</div>}
        {!!c.subscriptions?.length && (
          <div className="rows">
            {c.subscriptions.map((p) => (
              <div key={p.id} className="set-row">
                <div className="stack" style={{ gap: 2 }}><span style={{ fontWeight: 500 }}>{p.label}</span>
                  <span className="sub">added {ago(p.created_at)}{p.last_ok_at ? ` · last delivered ${ago(p.last_ok_at)}` : ''}{p.failures ? ` · ${p.failures} failed` : ''}</span></div>
                <button className="btn small danger" onClick={() => remove.mutate(p.id)}>Remove</button>
              </div>
            ))}
          </div>
        )}
        <div><button className="btn small" disabled={test.isPending} onClick={() => test.mutate()}>Send a test</button>
          {test.data && <span className="sub" style={{ marginLeft: 8 }}>{test.data.sent ? `Sent to ${test.data.sent} place${test.data.sent === 1 ? '' : 's'}.` : 'Nowhere to send it yet.'}</span>}</div>
      </section>

      <section className="set-section">
        <div className="set-title">What notifies you</div>
        <div className="rows">
          {Object.entries(c.event_labels ?? {}).map(([k, label]) => (
            <div key={k} className="set-row">
              <span>{label}</span>
              <button className="toggle" role="switch" aria-checked={!!c.events[k]} aria-label={label.split(':')[0]} onClick={() => put({ events: { ...c.events, [k]: !c.events[k] } })}>
                <span className={`switch ${c.events[k] ? 'on' : ''}`}><span /></span>
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-title">Quiet hours</div>
        <div className="sub" style={{ fontSize: 13 }}>Hold back everything except approvals and new sign-ins.</div>
        <div className="inline-edit">
          <TimePicker label="Quiet from" value={c.quiet_start ?? ''} onChange={(v) => put({ quiet_start: v || null })} hour12={hour12} clearable />
          <span className="sub">to</span>
          <TimePicker label="Quiet until" value={c.quiet_end ?? ''} onChange={(v) => put({ quiet_end: v || null })} hour12={hour12} clearable />
          {(c.quiet_start || c.quiet_end) && <button className="link-btn" onClick={() => put({ quiet_start: null, quiet_end: null })}>Clear</button>}
        </div>
      </section>

      <section className="set-section">
        <div className="set-title">ntfy (optional)</div>
        <div className="sub" style={{ fontSize: 13 }}>For installs without HTTPS: notifications also go to this ntfy topic URL (e.g. https://ntfy.sh/a-long-random-topic). Anyone who knows the topic can read it, so make it hard to guess.</div>
        <NtfyEdit value={c.ntfy_url ?? ''} onSave={(v) => put({ ntfy_url: v || null })} />
      </section>
      {save.error && <div className="error" role="alert">{problem(save.error)}</div>}
    </div>
  )
}

function NtfyEdit({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value)
  return (
    <form className="set-form" onSubmit={(e) => { e.preventDefault(); onSave(v.trim()) }}>
      <input className="input" value={v} onChange={(e) => setV(e.target.value)} placeholder="https://ntfy.sh/…" aria-label="ntfy topic URL" />
      <div><button className="btn" disabled={v.trim() === value}>Save</button></div>
    </form>
  )
}
