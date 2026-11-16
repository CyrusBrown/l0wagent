import { useState } from 'react'
import { useWebPage } from '../api/queries'

/** Whatever the assistant serves on its port 8787, in a sandboxed frame. The port is another
 * origin (agent-web strips your cookies on the way), and the frame's sandbox gives the page
 * an opaque origin besides. Layout from the mockup. */
export function WebPage() {
  const web = useWebPage()
  const [n, setN] = useState(0)
  const url = web.data?.url
  return (
    <div className="web-page">
      <div className="row-start" style={{ gap: 8 }}>
        <div className="url-bar"><span className="pill ok" style={{ fontSize: 10.5 }}>sandboxed</span><span className="ellipsis">{url ?? '…'}</span></div>
        <button className="btn" onClick={() => { setN(n + 1); void web.refetch() }}>Reload</button>
        {url && <a className="btn" href={url} target="_blank" rel="noopener noreferrer">Open in new tab</a>}
      </div>
      {web.data && !web.data.reachable ? (
        <div className="web-empty">
          <span className="mono" style={{ fontSize: 12, color: 'var(--muted)', background: 'var(--bg)', borderRadius: 5, padding: '4px 8px' }}>nothing on port 8787</span>
          <span className="sub">The assistant isn't serving a page right now. It can start one (e.g. from <code>system/boot.sh</code>) and it shows up here.</span>
        </div>
      ) : url && <iframe key={n} className="web-frame" title="The assistant's web page" src={url} sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads" referrerPolicy="no-referrer" />}
    </div>
  )
}
