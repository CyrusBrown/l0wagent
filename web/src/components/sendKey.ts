import type { KeyboardEvent } from 'react'

/** Composers: Enter sends, Shift+Enter (or Alt/Option+Enter) is a new line, ⌘/Ctrl+Enter also sends.
 * The same on touch devices (owner, 2026-11-18: Enter should always send). Never mid-IME composition. */
export function isSendKey(e: KeyboardEvent): boolean {
  if (e.key !== 'Enter' || e.nativeEvent.isComposing || e.keyCode === 229) return false
  if (e.metaKey || e.ctrlKey) return true
  return !e.shiftKey && !e.altKey
}
