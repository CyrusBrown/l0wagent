import { dehydrate, hydrate, type QueryClient } from '@tanstack/react-query'
import { useSyncExternalStore } from 'react'
import { CLIENT_HEADERS } from './client'

// Offline (M5): a local copy of what you've looked at (projects, threads, messages, the Inbox,
// shelves, status) in IndexedDB, and an outbox for what you write while offline: messages,
// new threads, answers and finished tasks. They're sent in order when the connection returns,
// each with its idempotency key, so a retry never duplicates. Approvals, secrets, settings,
// budget changes and promote requests are online only: their buttons are disabled offline.

const DB = 'l0wagent'
// Not `me` or `meta`: who you are must always come from the server (a cached `me` would look
// signed in after a sign-out).
const CACHED = ['projects', 'threads', 'thread', 'inbox', 'shelf', 'status', 'budget', 'calendar', 'runs']
const MAX_AGE = 7 * 86400_000

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => {
      req.result.createObjectStore('kv')
      req.result.createObjectStore('outbox', { keyPath: 'id', autoIncrement: true })
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function tx<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open()
  return new Promise((resolve, reject) => {
    const req = fn(db.transaction(store, mode).objectStore(store))
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

const available = () => typeof indexedDB !== 'undefined'

// ── the read cache ───────────────────────────────────────────────────────
export async function restoreCache(qc: QueryClient): Promise<void> {
  if (!available()) return
  try {
    const saved = await tx<{ at: number; state: Parameters<typeof hydrate>[1] } | undefined>('kv', 'readonly', (s) => s.get('cache'))
    if (saved && Date.now() - saved.at < MAX_AGE) hydrate(qc, saved.state)
  } catch { /* a private window or a blocked database: carry on without the cache */ }
}

export function persistCache(qc: QueryClient): () => void {
  if (!available()) return () => {}
  let timer: ReturnType<typeof setTimeout> | null = null
  const save = () => {
    timer = null
    const state = dehydrate(qc, { shouldDehydrateQuery: (q) => q.state.status === 'success' && CACHED.includes(String(q.queryKey[0])) })
    tx('kv', 'readwrite', (s) => s.put({ at: Date.now(), state }, 'cache')).catch(() => {})
  }
  return qc.getQueryCache().subscribe(() => { if (!timer) timer = setTimeout(save, 2000) })
}

export async function clearOffline(): Promise<void> {
  if (!available()) return
  await tx('kv', 'readwrite', (s) => s.clear()).catch(() => {})
  await tx('outbox', 'readwrite', (s) => s.clear()).catch(() => {})
}

// ── the outbox ───────────────────────────────────────────────────────────
export type OutboxItem =
  | { id?: number; kind: 'message'; thread: number; body: { body: string; upload_ids: number[]; idempotency_key: string }; at: string; error?: string }
  | { id?: number; kind: 'thread'; body: { title: string; project?: string | null; first_message?: string; idempotency_key: string }; at: string; error?: string }
  | { id?: number; kind: 'answer'; question: number; body: { value: unknown; note: string | null; idempotency_key: string }; at: string; error?: string }
  | { id?: number; kind: 'task'; task: number; body: { outcome: 'done' | 'declined'; note: string | null; idempotency_key: string }; at: string; error?: string }

let items: OutboxItem[] = []
const listeners = new Set<() => void>()
const changed = () => listeners.forEach((l) => l())

export async function loadOutbox(): Promise<void> {
  if (!available()) return
  try {
    items = await tx<OutboxItem[]>('outbox', 'readonly', (s) => s.getAll())
    changed()
  } catch { /* no IndexedDB: the outbox lives in memory */ }
}

export async function enqueue(item: OutboxItem): Promise<void> {
  try {
    item.id = Number(await tx('outbox', 'readwrite', (s) => s.add(item)))
  } catch {
    item.id = Date.now()
  }
  items = [...items, item]
  changed()
}

async function remove(id: number) {
  items = items.filter((i) => i.id !== id)
  changed()
  await tx('outbox', 'readwrite', (s) => s.delete(id)).catch(() => {})
}

export function useOutbox(): OutboxItem[] {
  return useSyncExternalStore((cb) => { listeners.add(cb); return () => listeners.delete(cb) }, () => items, () => items)
}

/** A fetch that failed because we're offline (not because the server said no). */
export const isNetworkError = (e: unknown) => !navigator.onLine || e instanceof TypeError

function target(i: OutboxItem): string {
  switch (i.kind) {
    case 'message': return `/api/threads/${i.thread}/messages`
    case 'thread': return '/api/threads'
    case 'answer': return `/api/questions/${i.question}/answer`
    case 'task': return `/api/tasks/${i.task}/resolve`
  }
}

let flushing = false

/** Send queued items in order. Stops at the first network failure (still offline); an item
 * the server refuses (say, a question someone already answered) is dropped with its error. */
export async function flushOutbox(qc: QueryClient): Promise<void> {
  if (flushing || !items.length || !navigator.onLine) return
  flushing = true
  try {
    for (const item of [...items]) {
      let res: Response
      try {
        res = await fetch(target(item), { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', ...CLIENT_HEADERS }, body: JSON.stringify(item.body) })
      } catch {
        return
      }
      if (res.status >= 500) return
      await remove(item.id!)
    }
    qc.invalidateQueries()
  } finally {
    flushing = false
  }
}
