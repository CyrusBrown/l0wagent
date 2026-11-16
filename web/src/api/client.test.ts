import { afterEach, describe, expect, it, vi } from 'vitest'
import { newKey } from './client'

describe('newKey', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('works without crypto.randomUUID (plain http on a LAN address is not a secure context)', () => {
    vi.stubGlobal('crypto', { getRandomValues: (a: Uint8Array) => { a.forEach((_, i) => { a[i] = (i * 37) & 0xff }); return a } })
    const k = newKey()
    expect(k).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })
})
