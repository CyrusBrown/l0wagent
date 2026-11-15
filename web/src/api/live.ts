import type { QueryClient } from '@tanstack/react-query'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { S } from './client'

/** Read the NDJSON live stream with fetch (not EventSource, so apps can send a token header). */
export async function* readEvents(signal: AbortSignal): AsyncGenerator<S['LiveEvent']> {
  const res = await fetch('/api/events', { credentials: 'include', signal })
  if (!res.ok || !res.body) throw new Error(`events: HTTP ${res.status}`)
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buf += value
    let nl: number
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (line) yield JSON.parse(line)
    }
  }
}

/** Which cached queries an event makes stale. Kept coarse on purpose: refetches are cheap. */
export function invalidateFor(qc: QueryClient, e: S['LiveEvent']) {
  const inv = (...keys: string[][]) => keys.forEach((queryKey) => qc.invalidateQueries({ queryKey }))
  switch (e.type) {
    case 'message':
    case 'thread':
    case 'unread':
      return inv(['threads'], ['thread'], ['projects'])
    case 'project':
      return inv(['projects'])
    case 'card':
    case 'question':
    case 'permission':
    case 'task':
    case 'suggestion':
    case 'promote':
    case 'security':
    case 'claim':
    case 'inbox_counts':
      return inv(['inbox'], ['thread'], ['threads'], ['projects'], ['status'])
    case 'run':
    case 'timeline':
    case 'status':
      return inv(['runs'], ['run'], ['status'])
    case 'job':
      return inv(['jobs'])
    case 'shelf':
    case 'upload':
      return inv(['shelf'], ['files'])
  }
}

export type LiveState = 'connecting' | 'live' | 'offline'

/** Keep one live connection open while signed in; reconnect with backoff. */
export function useLive(enabled: boolean): LiveState {
  const qc = useQueryClient()
  const [state, setState] = useState<LiveState>('connecting')
  useEffect(() => {
    if (!enabled) return
    const ctl = new AbortController()
    let delay = 1000
    ;(async () => {
      while (!ctl.signal.aborted) {
        try {
          for await (const e of readEvents(ctl.signal)) {
            if (e.type === 'hello') {
              setState('live')
              delay = 1000
              qc.invalidateQueries() // anything may have changed while disconnected
            } else invalidateFor(qc, e)
          }
        } catch {
          if (ctl.signal.aborted) return
        }
        setState('offline')
        await new Promise((r) => setTimeout(r, delay))
        delay = Math.min(delay * 2, 30_000)
      }
    })()
    return () => ctl.abort()
  }, [enabled, qc])
  return state
}
