import { type ReactNode, useEffect, useState } from 'react'
import type { RouteObject } from 'react-router'
import { ApiError } from '../api/client'
import { useMe, useMeta } from '../api/queries'
import { Calendar } from '../screens/Calendar'
import { Files } from '../screens/Files'
import { Home } from '../screens/Home'
import { Inbox } from '../screens/Inbox'
import { Login } from '../screens/Login'
import { NotPorted } from '../screens/NotPorted'
import { NewItem, Project, Projects } from '../screens/Projects'
import { Run, Runs } from '../screens/Runs'
import { Settings } from '../screens/Settings'
import { Setup } from '../screens/Setup'
import { Thread } from '../screens/Thread'
import { Tools } from '../screens/Tools'
import { WebPage } from '../screens/WebPage'
import { Shell } from './Shell'

/** The setup wizard on a fresh server (kept open until its last step), then the login
 * screen until /api/auth/me succeeds. */
function Gate({ children }: { children: ReactNode }) {
  const meta = useMeta()
  const me = useMe()
  const [wizard, setWizard] = useState(false)
  useEffect(() => { if (meta.data?.setup_complete === false) setWizard(true) }, [meta.data?.setup_complete])
  if (wizard) return <Setup onDone={() => setWizard(false)} />
  if (meta.isPending || me.isPending) return null
  if (meta.data?.setup_complete === false) return null
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
      { path: 'calendar', element: <Calendar /> },
      { path: 'tools', element: <Tools /> },
      { path: 'network', element: <NotPorted name="Network" milestone="M6" /> },
      { path: 'web', element: <WebPage /> },
      { path: 'settings', element: <Settings /> },
      { path: 'settings/:tab', element: <Settings /> },
      { path: '*', element: <NotPorted name="Not found" /> },
    ],
  },
]
