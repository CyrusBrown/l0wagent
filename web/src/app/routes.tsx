import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router'
import { ApiError } from '../api/client'
import { useMe } from '../api/queries'
import { Files } from '../screens/Files'
import { Home } from '../screens/Home'
import { Inbox } from '../screens/Inbox'
import { Login } from '../screens/Login'
import { NotPorted } from '../screens/NotPorted'
import { NewItem, Project, Projects } from '../screens/Projects'
import { Run, Runs } from '../screens/Runs'
import { Thread } from '../screens/Thread'
import { Shell } from './Shell'

/** Shows the login screen until /api/auth/me succeeds. */
function Gate({ children }: { children: ReactNode }) {
  const me = useMe()
  if (me.isPending) return null
  if (me.error instanceof ApiError && me.error.status === 401) return <Login />
  return children
}

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Gate><Shell /></Gate>,
    children: [
      { index: true, element: <Home /> },
      { path: 'inbox', element: <Inbox /> },
      { path: 'new', element: <NewItem /> },
      { path: 'projects', element: <Projects /> },
      { path: 'p/:slug', element: <Project /> },
      { path: 't/:id', element: <Thread /> },
      { path: 'runs', element: <Runs /> },
      { path: 'runs/:id', element: <Run /> },
      { path: 'files', element: <Files /> },
      { path: 'calendar', element: <NotPorted name="Calendar" milestone="M4" /> },
      { path: 'tools', element: <NotPorted name="Tools & Secrets" milestone="M6 (basic secrets in M2)" /> },
      { path: 'network', element: <NotPorted name="Network" milestone="M6" /> },
      { path: 'web', element: <NotPorted name="The assistant's web page" milestone="M3" /> },
      { path: 'settings', element: <NotPorted name="Settings" milestone="M1 (account) and M4 (budgets)" /> },
      { path: '*', element: <NotPorted name="Not found" /> },
    ],
  },
]
