import { useCallback, useEffect, useRef, useState } from 'react'

const PREFIX = 'l0w-draft:'

function load(key: string): string {
  try { return localStorage.getItem(PREFIX + key) ?? '' } catch { return '' }
}

/** Text that survives leaving the page: an unsent draft, kept per key in this browser only
 * (cleared when sent). Storage can be unavailable (private mode); then it's ordinary state. */
type Update = string | ((prev: string) => string)

export function useDraft(key: string): [string, (v: Update) => void, () => void] {
  const [text, setTextState] = useState(() => load(key))
  const keyRef = useRef(key)
  useEffect(() => {  // a different thread or project: show its own draft
    if (keyRef.current !== key) { keyRef.current = key; setTextState(load(key)) }
  }, [key])
  const setText = useCallback((u: Update) => {
    setTextState((prev) => {
      const v = typeof u === 'function' ? u(prev) : u
      try { if (v.trim()) localStorage.setItem(PREFIX + keyRef.current, v); else localStorage.removeItem(PREFIX + keyRef.current) } catch { /* storage unavailable */ }
      return v
    })
  }, [])
  const clear = useCallback(() => setText(''), [setText])
  return [text, setText, clear]
}
