import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router'
import { ApiError } from './api/client'
import { routes } from './app/routes'
import { applyStoredTheme } from './app/theme'
import './styles/app.css'

applyStoredTheme()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2 },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={createBrowserRouter(routes)} />
    </QueryClientProvider>
  </StrictMode>,
)
