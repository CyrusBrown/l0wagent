import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// opencode Go (M6): save a key, see the model behind each tier, move the worker onto it and
// wake the assistant: the run goes through opencode (the fake one in tests).
test('opencode Go: key, tier models, roles and a run on opencode', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/settings/providers')
  await page.getByRole('button', { name: '+ Add provider' }).click()
  await page.getByRole('button', { name: /^opencode Go/ }).click()
  await page.getByLabel('opencode Go key').fill('sk-go-e2e-0123456789abcdefghij')
  await page.getByRole('button', { name: 'Connect' }).click()
  await expect(page.getByText(/runs on opencode/).first()).toBeVisible()
  // Its enabled models, and the catalog to add more from.
  await expect(page.getByText('opencode-go/deepseek-v4-pro', { exact: false }).first()).toBeVisible()
  await page.getByRole('button', { name: '+ Add models' }).click()
  await page.getByLabel('Search models').fill('beam')
  const box = page.getByRole('checkbox').first()
  await box.click()  // saved on the server; the box follows once it's saved
  await expect(box).toBeChecked()
  await page.getByRole('button', { name: 'Done' }).click()
  await expect(page.getByRole('button', { name: /Move .*beam.* up/i })).toBeVisible()

  await page.goto('/settings/roles')
  // One searchable picker across providers; it saves right away.
  await page.getByRole('button', { name: 'worker model' }).click()
  await page.getByLabel('Search worker model').fill('beam')
  await page.getByRole('option', { name: /Beam/ }).first().click()
  await expect(page.getByRole('button', { name: 'worker model' })).toContainText(/Beam/)
  await expect(page.getByText('Saved')).toBeVisible()

  await page.goto('/runs')
  await page.getByRole('button', { name: 'Wake now' }).click()
  await expect(page).toHaveURL(/\/runs\/\d+/)
  await page.getByText('Open full timeline →').click()
  await expect(page.getByText(/^Model opencode-go\/beam-free/).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText('Run finished', { exact: false }).first()).toBeVisible({ timeout: 60_000 })

  // Put the worker back, so the server is as the other tests expect.
  await page.goto('/settings/roles')
  await page.getByRole('button', { name: 'worker model' }).click()
  await page.getByLabel('Search worker model').fill('sonnet')
  await page.getByRole('option', { name: /^Sonnet/ }).first().click()
  await expect(page.getByRole('button', { name: 'worker model' })).toContainText('Sonnet')
})
