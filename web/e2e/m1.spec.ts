import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { STACK_DIR } from '../playwright.config'

/** Restart control (kill -9 and start) through the dev stack. */
const restartControl = () => process.kill(Number(readFileSync(join(STACK_DIR, 'stack.pid'), 'utf8')), 'SIGUSR1')

import { PASSWORD } from './helpers'

// M1's "done when": you log in, start a manual run, watch its timeline live, restart
// control mid-run, and the run carries on.
test('set up, log in, wake, watch live, survive a control restart', async ({ page }) => {
  await page.goto('/')

  // First run: the setup wizard.
  await expect(page.getByRole('heading', { name: 'Create the owner' })).toBeVisible()
  await page.getByPlaceholder('Your name').fill('Mara')
  await page.getByPlaceholder('Username').fill('mara')
  await page.getByPlaceholder(/^Password/).fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByRole('heading', { name: 'Your timezone' })).toBeVisible()
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByRole('heading', { name: 'Connect a model provider' })).toBeVisible()
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByText('Model provider blocked without a lease')).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: 'Open l0wagent' }).click()
  await expect(page.getByRole('heading', { name: /Good (morning|afternoon|evening|night), Mara/ })).toBeVisible()

  // Sign out and back in.
  await page.context().clearCookies()
  await page.goto('/')
  await page.getByPlaceholder('Username').fill('mara')
  await page.getByPlaceholder('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByRole('heading', { name: /Mara/ })).toBeVisible()

  // Start a run and watch it live.
  await page.getByRole('link', { name: 'Runs' }).click()
  await page.getByRole('button', { name: 'Wake now' }).click()
  await expect(page).toHaveURL(/\/runs\/\d+$/)
  await page.getByRole('button', { name: 'Detailed' }).click()
  await expect(page.getByText('Tidying the journal index.')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('heading', { name: /A fake session/ })).toBeVisible()

  // Restart control mid-run: the page reconnects and the run carries on.
  restartControl()
  await page.getByRole('button', { name: 'Simple' }).click()
  await expect(page.getByText('Control restarted; re-attached to the run')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('Run finished')).toBeVisible({ timeout: 90_000 }) // after the live stream notices the restart
  await expect(page.locator('.pill', { hasText: 'done' })).toBeVisible()

  // The new sign-ins show up as Inbox cards and in Settings → Account.
  await page.goto('/settings/account')
  await expect(page.getByText('This device')).toBeVisible()
})
