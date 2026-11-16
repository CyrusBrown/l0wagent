import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// opencode Go (M6): save a key, see the model behind each tier, move the worker onto it and
// wake the assistant: the run goes through opencode (the fake one in tests).
test('opencode Go: key, tier models, roles and a run on opencode', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/settings/providers')
  await page.getByLabel('opencode Go key').fill('sk-go-e2e-0123456789abcdefghij')
  await page.getByRole('button', { name: 'Save key' }).nth(1).click()
  await expect(page.getByText(/runs on opencode/).first()).toBeVisible()
  const medium = page.getByLabel('Medium (sonnet) model')
  await expect(medium).toHaveValue(/^opencode-go\//)
  await medium.fill('glm')
  await page.getByRole('button', { name: 'Save models' }).click()
  await expect(page.getByRole('alert')).toContainText('opencode-go/')
  await medium.fill('opencode-go/beam-free')
  await page.getByRole('button', { name: 'Save models' }).click()
  await expect(page.getByRole('button', { name: 'Save models' })).toHaveCount(0)

  await page.goto('/settings/roles')
  await page.getByLabel('worker provider').selectOption({ label: 'opencode Go' })
  await expect(page.getByLabel('worker model')).toContainText('sonnet · beam-free')
  await page.getByRole('button', { name: 'Save roles' }).click()
  await expect(page.getByRole('button', { name: 'Save roles' })).toHaveCount(0)

  await page.goto('/runs')
  await page.getByRole('button', { name: 'Wake now' }).click()
  await expect(page).toHaveURL(/\/runs\/\d+/)
  await page.getByRole('button', { name: 'Detailed' }).click()
  await expect(page.getByText(/^Model opencode-go\//).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('Run finished', { exact: false }).first()).toBeVisible({ timeout: 60_000 })

  // Put the worker back, so the server is as the other tests expect.
  await page.goto('/settings/roles')
  await page.getByLabel('worker provider').selectOption({ label: 'Claude subscription' })
  await page.getByRole('button', { name: 'Save roles' }).click()
  await expect(page.getByRole('button', { name: 'Save roles' })).toHaveCount(0)
})
