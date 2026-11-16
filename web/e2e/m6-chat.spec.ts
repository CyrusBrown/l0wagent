import { expect, test } from '@playwright/test'
import { signIn } from './helpers'

// M6: the 1:1 chat. Start one from New, watch it read the assistant's threads through its
// tools, save a secret on a card without the model seeing it, and move the chat to the
// assistant as a thread. (The e2e stack runs chats on a scripted model.)
test('1:1 chat: tools, a secret card and moving it to the assistant', async ({ page, request }) => {
  await signIn(page, request)

  await page.goto('/new?chat=1')
  await page.getByPlaceholder('Start a chat. Replies stream right away.').fill('fake:tools what is the assistant up to?')
  await page.getByTitle('Start the chat (⌘↵)').click()
  await expect(page).toHaveURL(/\/c\/\d+/)
  await expect(page.getByRole('button', { name: /Listed threads · checked the status/ })).toBeVisible()
  await expect(page.locator('.theirs').getByText(/^Done/)).toBeVisible()
  await expect(page.locator('.side-chats').getByText('fake:tools what is the assistant up to?')).toBeVisible()

  await expect(page.getByTitle('Send (↵)')).toBeVisible()  // the reply is done (no Stop button)
  await page.getByLabel('Message', { exact: true }).fill('fake:secret connect github')
  await page.getByLabel('Message', { exact: true }).press('Enter')
  const card = page.getByLabel('Secure input')
  await expect(card).toContainText('GITHUB_TOKEN')
  await card.getByLabel('Value for GITHUB_TOKEN').fill('ghp_e2e_secret_value_123456')
  await card.getByRole('button', { name: 'Save' }).click()
  await expect(card).toContainText('done')
  await expect(page.getByText('The owner saved the secret GITHUB_TOKEN', { exact: false })).toBeVisible()
  await expect(page.getByText('ghp_e2e_secret_value_123456')).toHaveCount(0)

  await page.getByRole('button', { name: 'Move to the assistant…' }).click()
  await page.getByLabel('Thread title').fill('GitHub access for the assistant')
  await page.getByRole('button', { name: 'Move', exact: true }).click()
  await page.getByRole('link', { name: /Continued in the thread #\d+ “GitHub access for the assistant”/ }).click()
  await expect(page.getByRole('heading', { name: 'GitHub access for the assistant' })).toBeVisible()
  await expect(page.getByText('Handed over from a 1:1 chat', { exact: false })).toBeVisible()
})
