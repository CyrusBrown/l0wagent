const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

/** "12 min ago", "Wed", "Nov 3": short and human, like the mockup. */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  const s = Math.round((now - t) / 1000)
  if (s < 0) return until(iso, now)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  if (s < 6 * 86400) return new Date(iso).toLocaleDateString(undefined, { weekday: 'short' })
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function until(iso: string, now = Date.now()): string {
  const s = Math.round((new Date(iso).getTime() - now) / 1000)
  if (s < 3600) return rtf.format(Math.max(1, Math.round(s / 60)), 'minute')
  if (s < 86400) return rtf.format(Math.round(s / 3600), 'hour')
  return new Date(iso).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })
}

export const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })

export function duration(startIso: string, endIso?: string | null): string {
  const m = Math.round(((endIso ? new Date(endIso).getTime() : Date.now()) - new Date(startIso).getTime()) / 60000)
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return ''
  if (n < 1000) return `${n} B`
  if (n < 1e6) return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)} KB`
  return `${(n / 1e6).toFixed(1)} MB`
}

export const pts = (n: number | null | undefined) => (n == null ? '–' : Number.isInteger(n) ? String(n) : n.toFixed(1))

/** Map a typed link (l0wagent://thread/12) to an in-app route. Unknown links return null. */
export function routeFor(link: string): string | null {
  const m = /^l0wagent:\/\/(thread|project|run|file|chat)\/(.+)$/.exec(link)
  if (!m) return null
  const [, kind, rest] = m
  switch (kind) {
    case 'thread': return `/t/${rest.replace(/#.*$/, '')}`
    case 'project': return `/p/${rest}`
    case 'run': return `/runs/${rest.replace(/#.*$/, '')}`
    case 'file': {
      const [volume, ...path] = rest.split('/')
      const dir = path.slice(0, -1).join('/')
      return `/files?volume=${encodeURIComponent(volume)}&path=${encodeURIComponent(dir)}&file=${encodeURIComponent(path.at(-1) ?? '')}`
    }
    default: return null
  }
}
