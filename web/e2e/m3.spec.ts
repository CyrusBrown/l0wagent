import { expect, test } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { STACK_DIR } from '../playwright.config'
import { signIn } from './helpers'

// M3's "done when": the assistant shelves a script's output, it updates in the project's
// Files tab when the script reruns, and an uploaded file reaches the assistant.
test('upload, shelve live, rerun, and browse', async ({ page, request }) => {
  await signIn(page, request)

  // A project and a thread in it, with a CSV attached.
  await page.goto('/projects')
  await page.getByRole('button', { name: '+ New project' }).click()
  await page.getByPlaceholder('Name').fill('Garden')
  await page.getByRole('button', { name: 'Create' }).click()
  await page.getByRole('button', { name: 'New thread in project' }).click()
  await page.getByPlaceholder(/Start a thread in Garden/).fill('Summarize these readings, please.')
  await page.locator('input[type=file]').setInputFiles({ name: 'readings.csv', mimeType: 'text/csv', buffer: Buffer.from('sensor,moisture\na,30\nb,42\n') })
  await expect(page.locator('.file-chip', { hasText: 'readings.csv' })).toBeVisible()
  await page.getByTitle('Start the thread (⌘↵)').click()
  await expect(page.locator('.bubble').getByText('Summarize these readings, please.')).toBeVisible()

  // Reply now: the assistant reads the upload, shelves the script's output, reruns it.
  await page.getByRole('button', { name: 'Reply now' }).click()
  await expect(page.getByText("I summarized the readings; the result is on the project's Files tab (it stays live).")).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: 'Files' }).click()
  const panel = page.locator('.thread-files')
  await expect(panel.getByText('Readings summary').first()).toBeVisible()
  await expect(panel.getByText(/pass: 2 readings, total 72/)).toBeVisible()

  await page.goto('/p/garden?tab=files')
  await expect(page.locator('.shelf-row', { hasText: 'Readings summary' })).toContainText('live')
  await expect(page.locator('.shelf-preview').getByText('second pass: 2 readings, total 72')).toBeVisible({ timeout: 30_000 })
  await page.locator('.version-pick').selectOption({ index: 2 }) // Current, v2, v1
  await expect(page.locator('.shelf-preview').getByText('first pass: 2 readings, total 72')).toBeVisible()

  // The file browser: the upload as a table, and agent HTML only in a sandboxed frame on the files origin.
  mkdirSync(join(STACK_DIR, 'work', 'garden'), { recursive: true })
  writeFileSync(join(STACK_DIR, 'work', 'garden', 'page.html'), '<h1>Agent page</h1><script>document.title = "ran"</script>')
  await page.goto('/files?volume=work&path=_inbox/garden&file=readings.csv')
  await expect(page.getByRole('columnheader', { name: 'moisture' })).toBeVisible()
  await page.goto('/files?volume=work&path=garden&file=page.html')
  const frame = page.locator('iframe.frame-preview')
  await expect(frame).toHaveAttribute('sandbox', /allow-scripts/)
  const src = await frame.getAttribute('src')
  expect(new URL(src!).port).not.toBe(new URL(page.url()).port)
  await expect(page.frameLocator('iframe.frame-preview').getByRole('heading', { name: 'Agent page' })).toBeVisible()
})
