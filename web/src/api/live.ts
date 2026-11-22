import type { QueryClient } from '@tanstack/react-query'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { publishExtensionEvent } from '../ext/bus'
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

// A chat's reply as it streams (`chat_delta` events): the text so far, or null once it's stored.
const drafts = new Map<number, string | null>()
const draftSubs = new Set<() => void>()
export function setDraft(chatId: number, text: string | null) {
  drafts.set(chatId, text)
  draftSubs.forEach((f) => f())
}
export function useDraft(chatId: number): string | null {
  return useSyncExternalStore((cb) => { draftSubs.add(cb); return () => { draftSubs.delete(cb) } }, () => drafts.get(chatId) ?? null)
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
    case 'status':
      // A run starting or ending changes what a thread's composer offers (Reply now).
      qc.invalidateQueries({ predicate: (q) => q.queryKey[0] === 'thread' && q.queryKey[2] === 'delivery' })
      return inv(['runs'], ['run'], ['status'], ['system'])
    case 'timeline':
      return inv(['runs'], ['run'], ['status'], ['system'])
    case 'membership':
      return inv(['membership'])
    case 'friend':
    case 'friend_request':
      return inv(['friends'], ['friend-requests'], ['friend-breaker'], ['threads'])
    case 'job':
      return inv(['jobs'])
    case 'budget':
    case 'calendar':
      qc.invalidateQueries({ predicate: (q) => q.queryKey[0] === 'thread' && q.queryKey[2] === 'delivery' })
      return inv(['budget'], ['calendar'], ['status'])
    case 'shelf':
    case 'upload':
      return inv(['shelf'], ['files'])
    case 'chat':
      return inv(['chats'], ['chat'])
    case 'extension': {
      const d = (e.data ?? {}) as { ext?: string; type?: string; data?: unknown }
      if (d.type === 'changed') return inv(['extensions'], ['meta'])
      if (d.ext && d.type) publishExtensionEvent(d.ext, d.type, d.data)
      return
    }
    case 'chat_delta': {
      const d = e.data ?? {}
      const id = Number(d.chat_id)
      setDraft(id, (d.text as string | null) ?? null)
      if (d.text == null) inv(['chat', String(id)], ['chats'])
      return
    }
  }
}

export type LiveState = 'connecting' | 'live' | 'offline'

/** The server sends a heartbeat every 15 s. A connection silent for longer than this is
 * dead even if it never closed (e.g. a proxy holding it open after control restarted). */
export const SILENCE_MS = 40_000

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
        const conn = new AbortController()
        const onAbort = () => conn.abort()
        ctl.signal.addEventListener('abort', onAbort)
        let watchdog = setTimeout(() => conn.abort(), SILENCE_MS)
        try {
          for await (const e of readEvents(conn.signal)) {
            clearTimeout(watchdog)
            watchdog = setTimeout(() => conn.abort(), SILENCE_MS)
            if (e.type === 'hello') {
              setState('live')
              delay = 1000
              qc.invalidateQueries() // anything may have changed while disconnected
            } else invalidateFor(qc, e)
          }
        } catch {
          if (ctl.signal.aborted) return
        } finally {
          clearTimeout(watchdog)
          ctl.signal.removeEventListener('abort', onAbort)
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
