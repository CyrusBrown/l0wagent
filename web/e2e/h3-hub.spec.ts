import { createHmac } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { request as httpRequest } from 'node:http'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { API_PORT, HUB_PORT, STACK_DIR } from '../playwright.hub.config'
import { PASSWORD, signIn } from './helpers'

// H3: a fresh instance joins the fake hub from the UI, and is then reached through the hub and the tunnel.

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
function totp(secret: string): string {
  let bits = ''
  for (const ch of secret.replace(/=+$/, '').toUpperCase()) bits += B32.indexOf(ch).toString(2).padStart(5, '0')
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)))
  const msg = Buffer.alloc(8)
  msg.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / 30)))
  const h = createHmac('sha1', key).update(msg).digest()
  const o = h[h.length - 1] & 15
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, '0')
}

function newInvite(): string {
  return execFileSync('uv', ['run', '--quiet', '--project', 'dev/hub', 'python', '-m', 'fakehub', '--base-url', `http://hub.localhost:${HUB_PORT}`,
    '--state', join(STACK_DIR, 'hub', 'state.json'), 'invite'], { cwd: '..', encoding: 'utf8' }).trim()
}

function getVia(host: string, path: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const r = httpRequest({ host: '127.0.0.1', port: HUB_PORT, path, headers: { Host: host } }, (res) => { res.resume(); resolve(res.statusCode ?? 0) })
    r.on('error', reject)
    r.end()
  })
}

// Runs on its own stack (playwright.hub.config.ts).
test('a fresh instance joins the fake hub from the UI', async ({ page, request }) => {
  await signIn(page, request, `http://127.0.0.1:${API_PORT}`)
  await page.goto('/settings/hub')
  await expect(page.getByText('Not joined').first()).toBeVisible()

  await page.getByLabel('Invite').fill(newInvite())
  await page.getByRole('button', { name: 'Read invite' }).click()
  await expect(page.getByRole('alert').first()).toBeVisible()
  await expect(page.getByText("Two-step sign-in isn't on")).toBeVisible()
  await expect(page.getByRole('button', { name: 'Join', exact: true })).toBeDisabled()

  const h = { 'x-l0w-client': 'e2e' }
  const setup = await page.request.post('/api/account/totp/setup', { headers: h })
  expect(setup.ok()).toBeTruthy()
  const { secret } = await setup.json()
  let totpOn = false
  const en = await page.request.post('/api/account/totp/enable', { headers: h, data: { code: totp(secret) } })
  expect(en.ok(), await en.text()).toBeTruthy()
  totpOn = true
  try {

    await page.getByLabel('Invite').fill(newInvite())
    await page.getByRole('button', { name: 'Read invite' }).click()
    await expect(page.getByText("Two-step sign-in isn't on")).toHaveCount(0)
    await page.locator('label', { hasText: /^Name/ }).locator('input').fill('maraweb')
    await page.getByLabel(/I've made a backup/).check()
    await page.getByRole('button', { name: 'Join', exact: true }).click()

    await expect(page.getByTestId('recovery-key')).toContainText('l0w-recovery:', { timeout: 60_000 })
    await page.getByRole('button', { name: "I've saved it" }).click()

    await expect(page.getByText(/^maraweb@/).first()).toBeVisible()
    await expect(page.getByText('connected', { exact: false }).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('OK', { exact: true })).toBeVisible()

    const app = await page.locator('.set-row', { hasText: /^App/ }).getByRole('link').innerText()
    expect(await getVia(new URL(app).host, '/api/meta')).toBe(200)
  } finally {
    if (totpOn) await page.request.post('/api/account/totp/disable', { headers: h, data: { password: PASSWORD } })
  }
})
