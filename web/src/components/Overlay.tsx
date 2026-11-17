import { type ReactNode, useEffect, useRef } from 'react'
import { usePresence } from './motion'
import { useEscape } from './useEscape'

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'

/** A phone sheet: a bottom sheet (`side="bottom"`, with a drag handle that pulls it down to dismiss)
 * or a left drawer (`side="left"`). A scrim, Escape and a tap outside close it; it slides in and out,
 * traps Tab, takes focus when it opens and hands it back when it closes. */
export function Overlay({ open, onClose, side, label, className = '', children }: {
  open: boolean; onClose: () => void; side: 'bottom' | 'left'; label: string; className?: string; children: ReactNode
}) {
  const { mounted, closing } = usePresence(open, 240)
  const panel = useRef<HTMLDivElement>(null)
  const drag = useRef<{ y: number; dy: number } | null>(null)
  useEscape(open, onClose)

  useEffect(() => {
    if (!open || !mounted) return
    const prev = document.activeElement as HTMLElement | null
    panel.current?.focus({ preventScroll: true })
    return () => { if (prev?.isConnected) prev.focus({ preventScroll: true }) }
  }, [open, mounted])

  if (!mounted) return null

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'Tab' || !panel.current) return
    const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null)
    if (!items.length) { e.preventDefault(); return }
    const first = items[0], last = items[items.length - 1]
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
  }

  const handle = side === 'bottom' && (
    <div
      className="ov-handle"
      onPointerDown={(e) => { drag.current = { y: e.clientY, dy: 0 }; e.currentTarget.setPointerCapture(e.pointerId) }}
      onPointerMove={(e) => {
        const d = drag.current
        if (!d || !panel.current) return
        d.dy = Math.max(0, e.clientY - d.y)
        panel.current.style.transition = 'none'
        panel.current.style.transform = `translateY(${d.dy}px)`
      }}
      onPointerUp={() => {
        const d = drag.current
        drag.current = null
        if (!d || !panel.current) return
        panel.current.style.transition = ''
        panel.current.style.transform = ''
        if (d.dy > 70) onClose()
      }}
      onPointerCancel={() => { drag.current = null; if (panel.current) { panel.current.style.transition = ''; panel.current.style.transform = '' } }}
    ><span /></div>
  )

  return (
    <div className={`ov-scrim ${closing ? 'closing' : ''}`} onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div
        ref={panel} className={`ov-panel ov-${side} ${className}`} role="dialog" aria-modal="true" aria-label={label}
        tabIndex={-1} onKeyDown={onKeyDown}
      >
        {handle}
        {children}
      </div>
    </div>
  )
}
