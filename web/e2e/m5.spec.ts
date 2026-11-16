import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// M5: the installable app. Written offline, a message waits on this device and is sent when
// the connection returns; what you saw stays readable offline; notification settings exist.
test('offline outbox, cached threads, notification settings', async ({ page, request, context }) => {
  await signIn(page, request)

  await page.goto('/new')
  await page.getByPlaceholder('What should the assistant work on?').fill('Order more drip line')
  await page.getByTitle('Start the thread (⌘↵)').click()
  await expect(page.getByRole('heading', { name: 'Order more drip line' })).toBeVisible()
  const url = page.url()

  await context.setOffline(true)
  await expect(page.locator('.offline-bar')).toContainText('Offline')
  await page.getByPlaceholder(/Reply…/).fill('Twenty metres, 16 mm.')
  await page.getByTitle('Send (⌘↵)').click()
  await expect(page.locator('.mine.pending')).toContainText('Twenty metres, 16 mm.')
  await expect(page.getByText('Waiting for connection (on this device)').first()).toBeVisible()
  await expect(page.locator('.offline-bar')).toContainText('1 item waiting')

  await context.setOffline(false)
  await expect(page.locator('.mine.pending')).toHaveCount(0, { timeout: 15_000 })
  await expect(page.locator('.bubble').getByText('Twenty metres, 16 mm.')).toBeVisible()
  await page.reload()
  await expect(page.locator('.bubble').getByText('Twenty metres, 16 mm.')).toHaveCount(1)  // sent once
  expect(page.url()).toBe(url)

  await page.goto('/settings/notifications')
  await expect(page.getByRole('switch', { name: 'Approvals' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByRole('switch', { name: 'New sign-ins' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByRole('switch', { name: 'Messages' })).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('switch', { name: 'Messages' }).click()
  await page.reload()
  await expect(page.getByRole('switch', { name: 'Messages' })).toHaveAttribute('aria-checked', 'true')
})
