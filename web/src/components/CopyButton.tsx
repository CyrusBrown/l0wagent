import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { S } from '../api/client'
import { copyText } from './clipboard'
import { kindOf, rawUrl, type Source } from './Preview'
import { useEscape } from './useEscape'

const NOT_TEXT = ['image', 'pdf', 'audio', 'video', 'model']

type MenuPos = { left: number; width: number; maxHeight: number; top?: number; bottom?: number }

/** Copy a preview source's text (for text files) or its path; the menu says which was copied. */
export function CopyButton({ src, volume, className = 'btn' }: { src: Source; volume?: S['Listing']['volume']; className?: string }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<MenuPos | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const btn = useRef<HTMLButtonElement>(null)
  useEscape(open, () => setOpen(false))
  const kind = kindOf(src.path)
  const textual = kind !== 'binary' && !NOT_TEXT.includes(kind)
  const full = `/${src.kind === 'file' ? src.volume : volume ?? ''}/${src.path}`.replace(/\/+/g, '/')
  const flash = (msg: string) => { setDone(msg); setOpen(false); setTimeout(() => setDone(null), 1600) }
  const copy = async (what: 'contents' | 'path') => {
    if (what === 'path') return flash((await copyText(full)) ? 'Path copied' : 'Couldn\'t copy')
    try {
      const r = await fetch(rawUrl(src), { credentials: 'include' })
      if (!r.ok) throw new Error(String(r.status))
      flash((await copyText(await r.text())) ? 'Copied' : 'Couldn\'t copy')
    } catch { flash('Couldn\'t copy') }
  }
  const show = () => {
    const r = btn.current!.getBoundingClientRect()
    const width = Math.min(360, window.innerWidth - 16)
    const left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8))
    const maxHeight = Math.min(320, window.innerHeight - 16)
    const up = r.bottom + maxHeight + 8 > window.innerHeight && r.top > maxHeight + 8
    setPos(up
      ? { left, width, bottom: window.innerHeight - r.top + 6, maxHeight: Math.min(maxHeight, r.top - 14) }
      : { left, width, top: r.bottom + 6, maxHeight: Math.min(maxHeight, window.innerHeight - r.bottom - 14) })
    setOpen(true)
  }
  const toggle = () => { if (!textual) void copy('path'); else if (open) setOpen(false); else show() }
  return (
    <>
      <button ref={btn} className={`${className}${open ? ' pressed' : ''}`} onClick={toggle} aria-label="Copy"
        aria-haspopup={textual ? 'menu' : undefined} aria-expanded={textual ? open : undefined}>
        {done ?? (textual ? 'Copy ▾' : 'Copy path')}
      </button>
      {open && pos && createPortal(<>
        <div className="menu-scrim" onClick={() => setOpen(false)} />
        <div className="menu" style={{ position: 'fixed', left: pos.left, right: 'auto', width: pos.width, maxWidth: 'none', maxHeight: pos.maxHeight, top: pos.top, bottom: pos.bottom }}>
          <button onClick={() => copy('contents')}>Copy contents</button>
          <button onClick={() => copy('path')}>Copy path <span className="sub mono" style={{ display: 'block', overflowWrap: 'anywhere' }}>{full}</span></button>
        </div>
      </>, document.body)}
    </>
  )
}
