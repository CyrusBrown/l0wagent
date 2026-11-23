import { useEffect, useState } from 'react'
import type { LiveState } from '../api/live'
import { problem, useApplyUpdate, useCancelUpdate, useUpdateInfo } from '../api/queries'

const STEPS = [['waiting', 'Waiting for the assistant'], ['updating', 'Updating'], ['restarting', 'Restarting']] as const

const read = (key: string) => { try { return localStorage.getItem(key) === '1' } catch { return false } }
const write = (key: string) => { try { localStorage.setItem(key, '1') } catch { /* private mode */ } }

/** The mockup's update banner, and what "Update now" adds: a confirm step, the progress of an update started here (waiting for the
 * assistant, updating, restarting) and how it ended. The live stream drops while the app restarts; the update info is read every
 * few seconds meanwhile (queries.ts), so the banner finds the result by itself once the new app answers.
 * Without the ops container's update option, applying an update stays on the server (`./l0w update`, which waits for the current run). */
export function UpdateBanner({ live }: { live: LiveState }) {
  const u = useUpdateInfo().data
  const key = `l0w-update-dismissed-${u?.latest}`
  const resultKey = `l0w-update-result-${u?.last_result?.at}`
  const [hidden, setHidden] = useState(() => read(key))
  const [resultHidden, setResultHidden] = useState(() => read(resultKey))
  const [how, setHow] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const cancel = useCancelUpdate()
  const apply = useApplyUpdate()
  useEffect(() => { setHidden(read(key)) }, [key])
  useEffect(() => { setResultHidden(read(resultKey)) }, [resultKey])

  if (u?.applying) {
    const a = u.applying
    const at = STEPS.findIndex(([s]) => s === a.state)
    const away = a.state === 'restarting' && live === 'offline'
    return (
      <div className="banner upd" role="status" aria-live="polite">
        <span style={{ fontWeight: 500 }}>Updating to l0wagent {a.version}</span>
        <ol className="upd-steps" aria-label="Update progress">
          {STEPS.map(([s, label], i) => <li key={s} className={i < at ? 'done' : i === at ? 'now' : ''} aria-current={i === at ? 'step' : undefined}>{label}</li>)}
        </ol>
        <span className="muted-text ellipsis" style={{ minWidth: 0 }}>{away ? 'Restarting… the app reconnects by itself.' : a.step}</span>
        <span style={{ flex: 1 }} />
        {a.state === 'waiting' && <button className="btn" disabled={cancel.isPending} onClick={() => cancel.mutate()} title="Stop waiting; runs can start again">Cancel update</button>}
        {cancel.error && <span className="muted-text">{problem(cancel.error)}</span>}
      </div>
    )
  }
  if (u?.last_result && !resultHidden) {
    const r = u.last_result
    return (
      <div className={`banner upd ${r.ok ? '' : 'fail'}`} role="status">
        <span style={{ fontWeight: 500 }}>{r.ok ? `Updated to l0wagent ${r.version}` : `The update to ${r.version} failed`}</span>
        {r.error && <span className="ellipsis" style={{ minWidth: 0 }} title={r.error}>{r.error}</span>}
        <span style={{ flex: 1 }} />
        {r.ok && <button className="btn" onClick={() => window.location.reload()}>Reload the app</button>}
        <button className="x" aria-label="Dismiss" onClick={() => { write(resultKey); setResultHidden(true) }}>✕</button>
      </div>
    )
  }
  if (u?.draining_until) {
    return <div className="banner"><span style={{ fontWeight: 500 }}>Updating</span><span>Waiting for {u.waiting_for.length ? u.waiting_for.join('; ') : 'the assistant to be idle'}. No new runs start meanwhile.</span>
      <span style={{ flex: 1 }} />
      <button className="btn" disabled={cancel.isPending} onClick={() => cancel.mutate()} title="Clear this if ./l0w update isn't running any more">Cancel update</button>
    </div>
  }
  if (!u?.available || hidden) return null
  const summary = (u.notes ?? '').split('\n').map((l) => l.replace(/^[-#*\s]+/, '')).find((l) => l.trim()) ?? ''
  return (
    <div className="banner" role="status">
      <span style={{ fontWeight: 500 }}>l0wagent {u.latest} is available</span>
      {summary && <span className="ellipsis muted-text" style={{ minWidth: 0 }}>{summary}</span>}
      {u.url && <a href={u.url} target="_blank" rel="noreferrer">Release notes</a>}
      <span style={{ flex: 1 }} />
      {u.ops_docker ? (
        confirm ? (
          <span className="upd-confirm">
            <span>The assistant finishes what it's doing first; the app restarts and reconnects.</span>
            <button className="btn" disabled={apply.isPending} onClick={() => apply.mutate(u.latest!, { onSuccess: () => setConfirm(false) })}>{apply.isPending ? 'Starting…' : 'Update now'}</button>
            <button className="btn quiet" disabled={apply.isPending} onClick={() => setConfirm(false)}>Not now</button>
          </span>
        ) : <button className="btn" onClick={() => { apply.reset(); setConfirm(true) }}>Update now</button>
      ) : how
        ? <span>On the server, in the l0wagent folder: <code>./l0w update</code> (it waits for the current run)</span>
        : <button className="btn" onClick={() => setHow(true)}>Update after the current run</button>}
      {apply.error && <span className="upd-error" role="alert">{problem(apply.error)}</span>}
      <button className="x" aria-label="Dismiss" onClick={() => { write(key); setHidden(true) }}>✕</button>
    </div>
  )
}
