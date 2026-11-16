// Screenshot each mockup screen (by clicking through design/mockup/mockup.bundle.html) next to
// the app's matching route, at the same size, to compare them by eye.
//
//   1. Run the app on fake data on :5300:  (cd ../control && L0W_BACKEND=mock uv run uvicorn l0w_control.app:app --port 8300) &
//                                          L0W_API=http://127.0.0.1:8300 pnpm exec vite --port 5300 &
//   2. node scripts/compare-mockup.mjs <out dir> [home,inbox,…]     (THEME=light for light mode)
//
// Writes <screen>-mock.png and <screen>-app.png.
import { chromium } from '@playwright/test'
const [out, only] = [process.argv[2], process.argv[3]]
const theme = process.env.THEME ?? 'dark'
const b = await chromium.launch()
const V = { width: 1440, height: 900 }
const MOCK = new URL('../../design/mockup/mockup.bundle.html', import.meta.url).href
const nav = (label) => async (p) => p.locator('div').filter({ hasText: new RegExp(`^${label}`) }).first().click()
const screens = {
  home: [async () => {}, '/'],
  inbox: [async (p) => p.getByText('Inbox', { exact: true }).first().click(), '/inbox'],
  runs: [async (p) => p.getByText('Runs', { exact: true }).first().click(), '/runs'],
  run: [async (p) => { await p.getByText('Runs', { exact: true }).first().click(); await p.waitForTimeout(500); await p.getByText('#123').first().click() }, '/runs/123'],
  files: [async (p) => p.getByText('Files', { exact: true }).first().click(), '/files'],
  tools: [async (p) => p.getByText('Tools', { exact: true }).first().click(), '/tools'],
  thread: [async (p) => p.getByText('Conversations', { exact: true }).first().click(), '/t/1'],
  projects: [async (p) => p.getByText('All projects').first().click({ force: true }), '/projects'],
  calendar: [async (p) => { await p.getByText('More', { exact: true }).first().click(); await p.waitForTimeout(300); await p.getByText('Calendar', { exact: true }).first().click() }, '/calendar'],
  settings: [async (p) => { await p.locator('[title="Settings"]').first().click() }, '/settings'],
  new: [async (p) => { await p.getByText('+', { exact: true }).first().click() }, '/new'],
}
for (const [name, [go, route]] of Object.entries(screens)) {
  if (only && !only.split(',').includes(name)) continue
  try {
    const m = await b.newPage({ viewport: V })
    await m.goto(MOCK); await m.waitForTimeout(2500)
    await go(m); await m.waitForTimeout(1200)
    await m.screenshot({ path: `${out}/${name}-mock.png` })
    await m.close()
  } catch (e) { console.log('mock', name, String(e).slice(0, 160)) }
  try {
    const a = await b.newPage({ viewport: V })
    await a.addInitScript((t) => localStorage.setItem('l0w.theme', t), theme)
    await a.goto('http://localhost:5300' + route); await a.waitForTimeout(1800)
    await a.screenshot({ path: `${out}/${name}-app.png` })
    await a.close()
  } catch (e) { console.log('app', name, String(e).slice(0, 160)) }
}
await b.close()
console.log('done')
