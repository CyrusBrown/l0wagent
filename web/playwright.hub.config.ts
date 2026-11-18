import { defineConfig } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// The hub spec's own stack (control + the fake hub, gateway and tunnel service), so the hub's extra
// processes and the owner's two-step sign-in don't touch the other specs. Run by `pnpm e2e` after them.
export const STACK_DIR = join(tmpdir(), 'l0w-e2e-hub-stack')
export const API_PORT = 8101
const WEB_PORT = 5175
export const HUB_PORT = 8770

export default defineConfig({
  testDir: 'e2e',
  testMatch: /h3-hub/,
  timeout: 150_000,
  workers: 1,
  reporter: [['list']],
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: 'retain-on-failure' },
  webServer: [
    {
      command: `uv run python scripts/dev_stack.py --port ${API_PORT} --dir ${STACK_DIR} --hub --hub-port ${HUB_PORT}`,
      cwd: '../control',
      env: { L0W_CORS_ORIGINS: `http://localhost:${WEB_PORT}` },
      url: `http://127.0.0.1:${API_PORT}/api/meta`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `pnpm exec vite --port ${WEB_PORT} --strictPort`,
      env: { L0W_API: `http://127.0.0.1:${API_PORT}` },
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
    },
  ],
})
