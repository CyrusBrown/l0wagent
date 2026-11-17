import { getTimeFormat } from '../app/timeFormat'

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
const pad = (n: number) => String(n).padStart(2, '0')
const hour12 = () => getTimeFormat() === '12h'

function timeText(h: number, m: number, s?: number): string {
  const minute = pad(m)
  const second = s == null ? '' : `:${pad(s)}`
  if (!hour12()) return `${pad(h)}:${minute}${second}`
  return `${h % 12 || 12}:${minute}${second} ${h < 12 ? 'AM' : 'PM'}`
}

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

/** A future time, relative ("in 5 minutes"). Past times clamp to "in 1 minute": callers show those differently. */
export function until(iso: string, now = Date.now()): string {
  const s = Math.round((new Date(iso).getTime() - now) / 1000)
  if (s < 3600) return rtf.format(Math.max(1, Math.round(s / 60)), 'minute')
  if (s < 86400) return rtf.format(Math.round(s / 3600), 'hour')
  const d = new Date(iso)
  return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${timeText(d.getHours(), d.getMinutes())}`
}

export const clock = (iso: string) => {
  const d = new Date(iso)
  return timeText(d.getHours(), d.getMinutes())
}

export const clockSeconds = (iso: string) => {
  const d = new Date(iso)
  return timeText(d.getHours(), d.getMinutes(), d.getSeconds())
}

/** A clock time in a time zone, e.g. for the calendar. */
export function clockIn(iso: string, tz: string): string {
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
  const p = Object.fromEntries(f.formatToParts(new Date(iso)).map((x) => [x.type, x.value]))
  return timeText(Number(p.hour), Number(p.minute))
}

export function duration(startIso: string | null | undefined, endIso?: string | null): string {
  if (!startIso || Number.isNaN(Date.parse(startIso))) return '—'
  const m =Math.round(((endIso ? new Date(endIso).getTime() : Date.now()) - new Date(startIso).getTime()) / 60000)
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`
}

export function bytes(n: number | null | undefined): string {
  if (n == null) return ''
  if (n < 1000) return `${n} B`
  if (n < 1e6) return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)} KB`
  return `${(n / 1e6).toFixed(1)} MB`
}

/** "weekly 41% · 5-hour 18%" from a provider usage reading (until budgets exist, M4). */
export function usageText(u: { seven_day_pct?: number | null; five_hour_pct?: number | null } | null | undefined): string {
  if (!u || u.seven_day_pct == null) return 'usage unknown'
  return `weekly ${Math.round(u.seven_day_pct)}%` + (u.five_hour_pct != null ? ` · 5-hour ${Math.round(u.five_hour_pct)}%` : '')
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

/** The mockup's timestamps: "Today 02:00", "Sun 06:00" within the week, else "Nov 9 23:00". */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  const t = timeText(d.getHours(), d.getMinutes())
  const today = new Date()
  if (d.toDateString() === today.toDateString()) return `Today ${t}`
  if (Math.abs(today.getTime() - d.getTime()) < 6.5 * 86400_000) return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${t}`
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${t}`
}
