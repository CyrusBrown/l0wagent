import { type ReactNode, useRef, useState } from 'react'
import { copyText } from './clipboard'

/** A code box with a Copy button. With a `label` (a Markdown block's language) it gets a header bar
 * holding the label and the button; without one the button sits in the corner, shown on hover or
 * focus (always on touch screens). `text` is copied as given; without it, the box's own text is. */
export function CodeBox({ children, text, label, className }: { children: ReactNode; text?: string; label?: string; className?: string }) {
  const box = useRef<HTMLDivElement>(null)
  const [done, setDone] = useState<string | null>(null)
  const copy = async () => {
    const value = text ?? box.current?.querySelector('pre')?.textContent ?? ''
    setDone((await copyText(value.replace(/\n$/, ''))) ? 'Copied' : 'Couldn’t copy')
    setTimeout(() => setDone(null), 1400)
  }
  const button = <button type="button" className={`code-copy ${done ? 'done' : ''}`} onClick={copy} aria-label={done ?? 'Copy code'}>{done ?? 'Copy'}</button>
  return (
    <div ref={box} className={`code-box ${label != null ? 'headed' : ''} ${className ?? ''}`}>
      {label != null && <div className="code-head"><span>{label}</span>{button}</div>}
      {children}
      {label == null && button}
    </div>
  )
}
