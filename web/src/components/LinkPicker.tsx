import { useMemo, useState, type KeyboardEvent } from 'react'
import { useProjects, useThreads } from '../api/queries'

/** Typing @name or #name in a composer offers projects or threads; picking one inserts a
 * typed link ([Title](l0wagent://thread/12)). /file links arrive with the files browser (M3). */
export function useLinkPicker(text: string, setText: (t: string) => void, caret: number) {
  const threads = useThreads()
  const projects = useProjects()
  const [sel, setSel] = useState(0)
  const [dismissed, setDismissed] = useState<string | null>(null)
  const before = text.slice(0, caret)
  const m = /(^|\s)([@#])([^\s@#]*)$/.exec(before)
  const token = m ? m[2] + m[3] : null
  const items = useMemo(() => {
    if (!m || token === dismissed) return []
    const q = m[3].toLowerCase()
    if (m[2] === '#') return (threads.data ?? []).filter((t) => t.title.toLowerCase().includes(q)).slice(0, 6).map((t) => ({ label: t.title, sub: t.project ?? 'General', link: `l0wagent://thread/${t.id}` }))
    return (projects.data ?? []).filter((p) => p.name.toLowerCase().includes(q) || p.slug.includes(q)).slice(0, 6).map((p) => ({ label: p.name, sub: p.status, link: `l0wagent://project/${p.slug}` }))
  }, [m?.[2], m?.[3], threads.data, projects.data, dismissed]) // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (i: number) => {
    const it = items[i]
    if (!it || !m) return
    const start = caret - (m[2].length + m[3].length)
    setText(text.slice(0, start) + `[${it.label.replace(/[[\]]/g, '')}](${it.link}) ` + text.slice(caret))
    setSel(0)
  }
  /** Returns true if the key was handled by the picker. */
  const onKey = (e: KeyboardEvent): boolean => {
    if (!items.length) return false
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, items.length - 1)); return true }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); return true }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(Math.min(sel, items.length - 1)); return true }
    if (e.key === 'Escape') { setDismissed(token); return true }
    return false
  }
  const menu = items.length ? (
    <div className="link-picker" role="listbox">
      {items.map((it, i) => (
        <button key={it.link} role="option" aria-selected={i === sel} className={i === sel ? 'sel' : ''} onMouseDown={(e) => { e.preventDefault(); pick(i) }}>
          <span>{it.label}</span><small>{it.sub}</small>
        </button>
      ))}
    </div>
  ) : null
  return { menu, onKey }
}
