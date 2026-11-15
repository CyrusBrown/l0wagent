import { useSyncExternalStore } from 'react'

const subscribe = (cb: () => void) => {
  window.addEventListener('online', cb)
  window.addEventListener('offline', cb)
  return () => {
    window.removeEventListener('online', cb)
    window.removeEventListener('offline', cb)
  }
}

/** Approvals, secrets, settings and budget changes are online-only: their buttons use this. */
export const useOnline = () => useSyncExternalStore(subscribe, () => navigator.onLine, () => true)
