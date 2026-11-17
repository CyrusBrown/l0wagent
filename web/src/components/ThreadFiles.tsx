import { useEffect, useRef, useState, type CSSProperties } from 'react'
import type { S } from '../api/client'
import { ShelfView } from './ShelfView'
import { useEscape } from './useEscape'
import { useIsPhone } from './useIsPhone'

export const THREAD_FILES_KEY = 'l0w.threadFilesWidth'
export const THREAD_FILES_MIN = 280
export const THREAD_FILES_MAX = 560
export const THREAD_FILES_DEFAULT = 360

const clamp = (n: number) => Math.min(THREAD_FILES_MAX, Math.max(THREAD_FILES_MIN, Math.round(n)))

export function readThreadFilesWidth() {
  try {
    const raw = localStorage.getItem(THREAD_FILES_KEY)
    const n = raw == null ? NaN : Number(raw)
    return Number.isFinite(n) ? clamp(n) : THREAD_FILES_DEFAULT
  } catch {
    return THREAD_FILES_DEFAULT
  }
}

export function ThreadFiles({ project, onClose, onLink, closing }: { project: string; onClose: () => void; onLink: (item: S['ShelfItem']) => void; closing?: boolean }) {
  // On a phone the panel is a full-screen sheet: a dialog that Escape closes.
  const phone = useIsPhone()
  useEscape(phone && !closing, onClose)
  const [width, setWidth] = useState(readThreadFilesWidth)
  const [resizing, setResizing] = useState(false)
  const drag = useRef<{ x: number; w: number } | null>(null)

  useEffect(() => {
    try { localStorage.setItem(THREAD_FILES_KEY, String(width)) } catch {}
  }, [width])

  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current
      if (!d) return
      setWidth(clamp(d.w + d.x - e.clientX))
    }
    const stop = () => {
      if (!drag.current) return
      drag.current = null
      setResizing(false)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
    }
  }, [])

  const nudge = (by: number) => setWidth((w) => clamp(w + by))

  return (
    <aside id="thread-files-panel" className={`thread-files${resizing ? ' resizing' : ''}${closing ? ' closing' : ''}`}
      {...(phone ? { role: 'dialog', 'aria-modal': true, 'aria-label': 'Project files' } : {})}
      style={{ '--thread-files-w': `${width}px` } as CSSProperties}>
      <div
        className="thread-files-handle" role="separator" aria-label="Resize project files" aria-orientation="vertical"
        aria-controls="thread-files-panel" aria-valuemin={THREAD_FILES_MIN} aria-valuemax={THREAD_FILES_MAX} aria-valuenow={width}
        aria-valuetext={`${width} pixels`} tabIndex={0}
        onPointerDown={(e) => {
          if (e.button !== 0) return
          e.preventDefault()
          e.currentTarget.focus()
          drag.current = { x: e.clientX, w: width }
          setResizing(true)
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft') { e.preventDefault(); nudge(16) }
          else if (e.key === 'ArrowRight') { e.preventDefault(); nudge(-16) }
          else if (e.key === 'Home') { e.preventDefault(); setWidth(THREAD_FILES_MIN) }
          else if (e.key === 'End') { e.preventDefault(); setWidth(THREAD_FILES_MAX) }
        }}
      />
      <div className="row-between" style={{ padding: '14px 16px', borderBottom: '1px solid var(--lineSoft)' }}>
        <div className="stack" style={{ gap: 2 }}><span style={{ font: '500 14px var(--sans)' }}>Project files</span><span className="sub">{project} · shelf</span></div>
        <button className="icon-btn" aria-label="Close files" onClick={onClose}>✕</button>
      </div>
      <ShelfView project={project} compact onLink={onLink} />
    </aside>
  )
}
