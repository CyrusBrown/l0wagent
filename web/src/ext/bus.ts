// Live "extension" events: the live stream (api/live.ts) publishes here, and the host object's
// `on(type, fn)` subscribes. Kept apart from live.ts so extensions never touch the query cache.

type Fn = (data: unknown) => void
const subs = new Map<string, Set<Fn>>()
const key = (ext: string, type: string) => `${ext}\u0000${type}`

export function publishExtensionEvent(ext: string, type: string, data: unknown) {
  subs.get(key(ext, type))?.forEach((fn) => {
    try { fn(data) } catch (e) { console.error(`extension ${ext}: handler for ${type} threw`, e) }
  })
}

export function subscribeExtensionEvent(ext: string, type: string, fn: Fn): () => void {
  const k = key(ext, type)
  let set = subs.get(k)
  if (!set) subs.set(k, (set = new Set()))
  set.add(fn)
  return () => { set.delete(fn) }
}
