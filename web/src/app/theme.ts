import { useCallback, useEffect, useState } from 'react'

export type ThemePref = 'light' | 'dark' | 'system'
const KEY = 'l0w.theme'

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'system'
  } catch {
    return 'system'
  }
}

const systemDark = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
const resolve = (p: ThemePref) => (p === 'system' ? (systemDark() ? 'dark' : 'light') : p)

/** The browser's / installed app's toolbar takes the header's color (--side in each theme). */
function paintChrome(theme: string) {
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#1f1e1d' : '#f3f1ea')
}

export function applyStoredTheme() {
  const theme = resolve(read())
  document.documentElement.dataset.theme = theme
  paintChrome(theme)
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(read)
  const theme = resolve(pref)
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    paintChrome(theme)
    try { localStorage.setItem(KEY, pref) } catch { /* private mode */ }
  }, [pref, theme])
  const toggle = useCallback(() => setPref(theme === 'dark' ? 'light' : 'dark'), [theme])
  return { theme, pref, setPref, toggle }
}
