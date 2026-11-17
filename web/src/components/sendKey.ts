import type { KeyboardEvent } from 'react'

const touch = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches

/** Composers: Enter sends and Shift+Enter is a new line. ⌘/Ctrl+Enter always sends. On a touch keyboard
 * (no Shift+Return) Return stays a new line and the send button sends. Never mid-IME composition. */
export function isSendKey(e: KeyboardEvent): boolean {
  if (e.key !== 'Enter' || e.nativeEvent.isComposing || e.keyCode === 229) return false
  if (e.metaKey || e.ctrlKey) return true
  return !e.shiftKey && !e.altKey && !touch()
}
