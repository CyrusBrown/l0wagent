import { useEffect, useRef } from 'react'

type Entry = { close: () => void }

const stack: Entry[] = []
let listening = false

function onKey(e: KeyboardEvent) {
  if (e.key !== 'Escape' || e.defaultPrevented) return
  const top = stack[stack.length - 1]
  if (!top) return
  e.preventDefault()
  top.close()
}

export function useEscape(open: boolean, onClose: () => void) {
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    if (!open) return
    const entry: Entry = { close: () => close.current() }
    stack.push(entry)
    if (!listening) {
      window.addEventListener('keydown', onKey)
      listening = true
    }
    return () => {
      const i = stack.lastIndexOf(entry)
      if (i >= 0) stack.splice(i, 1)
      if (!stack.length && listening) {
        window.removeEventListener('keydown', onKey)
        listening = false
      }
    }
  }, [open])
}
