import { useSyncExternalStore } from 'react'

/** The phone layout's breakpoint, the same one app.css uses. */
export const PHONE_QUERY = '(max-width: 760px)'

const mq = () => (typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_QUERY) : null)

function subscribe(cb: () => void) {
  const m = mq()
  m?.addEventListener('change', cb)
  return () => m?.removeEventListener('change', cb)
}

/** True on a phone-width viewport. Phone-only pieces (tab bar, drawers, sheets) mount only then, so
 * desktop never carries a second copy of the navigation. */
export function useIsPhone() {
  return useSyncExternalStore(subscribe, () => mq()?.matches ?? false, () => false)
}
