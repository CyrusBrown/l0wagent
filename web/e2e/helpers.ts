import type { APIRequestContext, Page } from '@playwright/test'
import { expect } from '@playwright/test'

export const USER = 'mara'
export const PASSWORD = 'a long enough password'
export const API = 'http://127.0.0.1:8100'

/** Sign in through the UI, creating the owner first if this server is fresh. */
export async function signIn(page: Page, request: APIRequestContext, api = API) {
  const meta = await (await request.get(`${api}/api/meta`)).json()
  if (!meta.setup_complete) {
    await request.post(`${api}/api/setup/owner`, { data: { username: USER, password: PASSWORD, display_name: 'Mara', timezone: 'UTC' } })
  }
  await page.goto('/')
  await page.getByPlaceholder('Username').fill(USER)
  await page.getByPlaceholder('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Continue' }).click()
  await expect(page.getByRole('heading', { name: /Mara/ })).toBeVisible()
}
