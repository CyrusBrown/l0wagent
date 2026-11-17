import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// M2's "done when": you message the assistant, press Reply now, and it replies, claims a
// task and files a question you answer in place, all live.
test('message, Reply now, and answer its question in place', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/new')
  await page.getByPlaceholder('What should the assistant work on?').fill('Can you run the pump test this weekend?')
  await page.getByTitle('Start the thread (Enter)').click()
  await expect(page.getByRole('heading', { name: 'Can you run the pump test this weekend?' })).toBeVisible()
  await expect(page.getByText(/^Queued for the (next wakeup|assistant's next run)/)).toBeVisible()

  await page.getByRole('button', { name: 'Reply now' }).click()
  await expect(page.getByText('On it. One question first, so I schedule it right.')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.claim-card').getByText('In progress')).toBeVisible()
  await expect(page.getByText('Which day should I run the pump test?')).toBeVisible()
  await expect(page.getByText(/Working now|Waiting for your reply/)).toBeVisible()

  await page.getByRole('button', { name: 'Sunday' }).click()
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('You answered: Sunday')).toBeVisible()
  await expect(page.getByText('Got it: Sunday. The pump test is scheduled.')).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.claim-card').getByText('Pump test scheduled for Sunday.')).toBeVisible()

  // Typed links: #thread offers threads and inserts an l0wagent:// link that opens in the app.
  const box = page.getByPlaceholder(/Reply…/)
  await box.fill('Notes are in #pump')
  await page.getByRole('option', { name: /Can you run the pump test/ }).click()
  await expect(box).toHaveValue(/\[Can you run the pump test this weekend\?\]\(l0wagent:\/\/thread\/\d+\)/)
  await page.getByTitle('Send (Enter; Shift+Enter for a new line)').click()
  await expect(page.locator('.bubble a', { hasText: 'Can you run the pump test this weekend?' })).toHaveAttribute('href', /\/t\/\d+$/)

  // The thread shows up in the sidebar, read; the run lists what happened.
  await expect(page.locator('.side').getByText('Can you run the pump test this weekend?')).toBeVisible()
  await page.locator('.claim-card').getByRole('link', { name: /run #\d+/ }).click()
  await expect(page.getByRole('heading', { name: /Run #\d+/ })).toBeVisible()
  await expect(page.locator('.change-row', { hasText: 'Asks filed' })).toContainText('1 item')
})
