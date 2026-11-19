import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// A project with a long name keeps its "open project" link inside the sidebar (its name ellipsizes instead).
test('a long project name keeps its open link', async ({ page, request }) => {
  await signIn(page, request)
  const name = 'A project with a very long name that would never fit in the sidebar at all'
  const r = await page.request.post('/api/projects', {
    headers: { 'X-L0W-Client': 'web' },
    data: { name, idempotency_key: `e2e-long-${Date.now()}` },
  })
  expect(r.ok()).toBeTruthy()
  await page.goto('/general')
  const open = page.getByRole('link', { name: `Open ${name}` })
  await expect(open).toBeVisible()
  const side = (await page.locator('.side').boundingBox())!
  const link = (await open.boundingBox())!
  expect(link.x + link.width).toBeLessThanOrEqual(side.x + side.width)
  await open.click()
  await expect(page).toHaveURL(/\/p\//)
})
