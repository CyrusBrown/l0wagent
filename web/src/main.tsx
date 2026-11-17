import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router'
import { ApiError } from './api/client'
import { flushOutbox, loadOutbox, persistCache, restoreCache } from './api/offline'
import { routes } from './app/routes'
import { applyStoredTheme } from './app/theme'
import './styles/app.css'
import './styles/phone.css'

applyStoredTheme()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2 },
    // Mutations decide for themselves what happens offline: creates go to the outbox, and
    // online-only actions (approvals, secrets, settings, budgets) have their buttons disabled.
    mutations: { networkMode: 'always' },
  },
})

// Offline: what you last saw, and what you wrote while offline (sent when back online).
await Promise.race([Promise.all([restoreCache(queryClient), loadOutbox()]), new Promise((r) => setTimeout(r, 1500))])
persistCache(queryClient)
window.addEventListener('online', () => flushOutbox(queryClient))
void flushOutbox(queryClient)

// The installable app: the service worker caches the app shell and shows push notifications.
// Only in production builds, and only where browsers allow it (HTTPS, or localhost).
if (import.meta.env.PROD && 'serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={createBrowserRouter(routes)} />
    </QueryClientProvider>
  </StrictMode>,
)
