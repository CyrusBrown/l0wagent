import { useQueryClient } from '@tanstack/react-query'
import { discardItem, retryItem, useOutbox, type OutboxItem } from '../api/offline'

const what = (i: OutboxItem) => ({ message: 'A message', thread: 'A new thread', answer: 'An answer', task: 'A finished task' })[i.kind]

/** Queued items the server refused: kept with the reason, until you retry or discard them. */
export function OutboxFailures() {
  const qc = useQueryClient()
  const failed = useOutbox().filter((i) => i.error)
  if (!failed.length) return null
  return (
    <div className="outbox-failed" role="alert">
      {failed.map((i) => (
        <div key={i.id} className="row-start" style={{ gap: 10, flexWrap: 'wrap' }}>
          <span>{what(i)} from this device wasn't sent: {i.error}</span>
          <span style={{ flex: 1 }} />
          <button className="btn small" onClick={() => void retryItem(i.id!, qc)}>Retry</button>
          <button className="btn small" onClick={() => void discardItem(i.id!)}>Discard</button>
        </div>
      ))}
    </div>
  )
}
