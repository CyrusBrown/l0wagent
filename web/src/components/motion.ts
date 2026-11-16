import { useEffect, useState } from 'react'

/** Keeps something mounted while its exit animation plays. `open` drives it; render while
 * `mounted`, and add the `closing` class (or `data-closing`) to play the exit. */
export function usePresence(open: boolean, ms = 160): { mounted: boolean; closing: boolean } {
  const [mounted, setMounted] = useState(open)
  const [closing, setClosing] = useState(false)
  useEffect(() => {
    if (open) {
      setMounted(true)
      setClosing(false)
      return
    }
    if (!mounted) return
    setClosing(true)
    const t = setTimeout(() => { setMounted(false); setClosing(false) }, ms)
    return () => clearTimeout(t)
  }, [open])  // eslint-disable-line react-hooks/exhaustive-deps
  return { mounted, closing }
}
