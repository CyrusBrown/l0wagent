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

export function applyStoredTheme() {
  document.documentElement.dataset.theme = resolve(read())
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(read)
  const theme = resolve(pref)
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem(KEY, pref) } catch { /* private mode */ }
  }, [pref, theme])
  const toggle = useCallback(() => setPref(theme === 'dark' ? 'light' : 'dark'), [theme])
  return { theme, pref, setPref, toggle }
}
