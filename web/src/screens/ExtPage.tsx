import { useParams } from 'react-router'
import { ExtSlot } from '../ext/ExtElement'
import { pagePath, pages } from '../ext/registry'
import { useExtensions } from '../ext/useExtensions'
import { NotPorted } from './NotPorted'

/** A page an extension adds (reached from More ▾): /x/<extension>/<page path>. */
export function ExtPage() {
  const { ext, '*': path = '' } = useParams()
  const exts = useExtensions()
  if (exts.isPending) return <div className="page" />
  const hit = pages(exts.data).find((p) => p.ext.name === ext && pagePath(p.page.path) === path.replace(/\/+$/, ''))
  if (!hit) return <NotPorted name="Not found" />
  return (
    <div className="page ext-page">
      <ExtSlot key={hit.to} ext={hit.ext} element={hit.page.element} context={{ path: hit.page.path }} />
    </div>
  )
}
