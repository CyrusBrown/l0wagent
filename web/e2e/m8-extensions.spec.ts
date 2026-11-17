import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// M8: an extension (the example plant log) is switched on in Settings, adds a page under More,
// and its element works against its own routes; and Settings → Advanced keeps a changed value.
test('extensions: switch on, use the page, switch off; Advanced settings persist', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/settings/extensions')
  await expect(page.getByText(/^Plant log/).first()).toBeVisible()
  const sw = page.getByRole('switch', { name: 'Plant log enabled' })
  try {
    if ((await sw.getAttribute('aria-checked')) !== 'true') await sw.click()
    await expect(sw).toHaveAttribute('aria-checked', 'true')

    await page.getByRole('button', { name: /More/ }).click()
    await page.getByRole('link', { name: 'Plants' }).first().click()
    await expect(page).toHaveURL(/\/x\/plantlog\/plants/)
    await page.getByPlaceholder('Plant', { exact: true }).fill('Fern')
    await page.getByPlaceholder('Note').fill('new frond')
    await page.getByRole('button', { name: 'Add note' }).click()
    await expect(page.getByRole('cell', { name: /Fern/ })).toBeVisible()
    await expect(page.getByText('new frond')).toBeVisible()
    await page.getByRole('button', { name: 'Watered' }).click()
    await expect(page.getByRole('row', { name: /Fern/ }).getByText('never')).toHaveCount(0)

    await page.goto('/settings/advanced')
    const wait = page.getByLabel('Wait for your reply')
    await wait.fill('7')
    await wait.blur()
    await expect(page.getByText('Saved')).toBeVisible()
    await page.reload()
    await expect(page.getByLabel('Wait for your reply')).toHaveValue('7')
  } finally {
    await page.goto('/settings/extensions')
    const off = page.getByRole('switch', { name: 'Plant log enabled' })
    if ((await off.getAttribute('aria-checked')) === 'true') await off.click()
    await expect(off).toHaveAttribute('aria-checked', 'false')
  }
})
