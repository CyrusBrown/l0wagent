import { defineConfig } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// Browser tests against the real backend: control + the runner (fake claude) + egress,
// started without Docker by control/scripts/dev_stack.py, and the Vite dev server.
export const STACK_DIR = join(tmpdir(), 'l0w-e2e-stack')
const API_PORT = 8100
const WEB_PORT = 5174

export default defineConfig({
  testDir: 'e2e',
  timeout: 120_000,
  workers: 1,
  reporter: [['list']],
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: 'retain-on-failure' },
  webServer: [
    {
      command: `uv run python scripts/dev_stack.py --port ${API_PORT} --dir ${STACK_DIR} --steps 10 --delay 1.2 --hold 0.2`,
      cwd: '../control',
      env: { L0W_CHAT_FAKE: '1' },  // 1:1 chats use the scripted model (no provider key in tests)
      url: `http://127.0.0.1:${API_PORT}/api/meta`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm exec vite --port ${WEB_PORT} --strictPort`,
      env: { L0W_API: `http://127.0.0.1:${API_PORT}` },
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
    },
  ],
})
