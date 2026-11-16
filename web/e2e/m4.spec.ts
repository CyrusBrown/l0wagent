import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// M4: the assistant plans within the budget you set. Pick a level, give a standing instruction,
// wake it: it checks its budget, asks the reviewer, registers a long job and plans its next
// wakeup, which shows on the Calendar; the run reads clearly in both views.
test('levels, a wakeup that plans the next one, the calendar and both run views', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/settings/budgets')
  await page.getByRole('button', { name: /Busy\s*400 pts \/ week/ }).click()
  await expect(page.getByRole('button', { name: /Busy\s*400 pts \/ week/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText(/400 of 400 pts left|of 400 pts left/).first()).toBeVisible()

  await page.goto('/settings/instructions')
  await page.getByLabel('Standing instructions').fill('fake:m4 Keep the garden sensor exports running nightly.')
  await page.getByLabel('Standing instructions').blur()  // saved when you leave the box
  await expect(page.getByText('Saved', { exact: true })).toBeVisible()

  // The tests share one server: wait for an earlier test's run (e.g. M3's reply) to finish.
  await expect.poll(async () => {
    const runs: { status: string }[] = await (await page.request.get('/api/runs')).json()
    return runs.some((r) => r.status === 'starting' || r.status === 'running')
  }, { timeout: 60_000 }).toBe(false)
  await page.goto('/runs')
  await page.getByRole('button', { name: 'Wake now' }).click()
  await expect(page).toHaveURL(/\/runs\/\d+/)
  await expect(page.getByText('Run finished', { exact: false }).first()).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/^Reviewer: Nightly/)).toBeVisible()
  await expect(page.getByText(/Planned a wakeup/).first()).toBeVisible()
  await page.getByText('Open full timeline →').click()
  await expect(page.getByText(/To the reviewer \(ask\)/)).toBeVisible()

  // Its plan is on the calendar (it may fall in next week), with the budget beside it.
  await page.goto('/calendar')
  await expect(page.getByText('Budget left this week')).toBeVisible()
  const reason = 'Check the sensor export and summarize it'
  if (!(await page.getByText(reason).first().isVisible())) await page.getByRole('button', { name: 'Next week' }).click()
  await expect(page.getByText(reason).first()).toBeVisible()

  // The long job, with its log.
  await page.goto('/runs?tab=jobs')
  await expect(page.getByText('Nightly sensor export')).toBeVisible()
})
