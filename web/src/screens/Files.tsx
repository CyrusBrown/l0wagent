import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import type { S } from '../api/client'
import { useListing } from '../api/queries'
import { ago, bytes } from '../components/format'
import { Markdown } from '../components/Markdown'

type Volume = S['Listing']['volume']

/** Read-only browser over the agent's storage. Previews here are text-only for M0; the
 *  full previewers (and agent HTML on a separate origin) are M3. */
export function Files() {
  const [params, setParams] = useSearchParams()
  const volume = (params.get('volume') === 'work' ? 'work' : 'workspace') as Volume
  const path = params.get('path') ?? ''
  const file = params.get('file')
  const listing = useListing(volume, path)
  const go = (p: { volume?: Volume; path?: string; file?: string | null }) => {
    const next = { volume: p.volume ?? volume, path: p.path ?? path, file: p.file === undefined ? file : p.file }
    setParams(Object.fromEntries(Object.entries(next).filter(([, v]) => v)) as Record<string, string>)
  }
  const parts = path.split('/').filter(Boolean)
  return (
    <div className="page">
      <div className="page-head">
        <h1>Files</h1>
        <div className="seg">
          {(['workspace', 'work'] as const).map((v) => (
            <button key={v} className={volume === v ? 'on' : ''} onClick={() => go({ volume: v, path: '', file: null })}>/{v}</button>
          ))}
          <button disabled title="Shared spaces come later, with the hub">/shared</button>
        </div>
      </div>
      <div className="crumbs">
        <button onClick={() => go({ path: '', file: null })}>/{volume}</button>
        {parts.map((p, i) => <span key={i}>/<button onClick={() => go({ path: parts.slice(0, i + 1).join('/'), file: null })}>{p}</button></span>)}
        {listing.data?.git && <span className="pill info" style={{ marginLeft: 8 }}>git</span>}
      </div>
      <div className="files">
        <div className="card" style={{ padding: 6, gap: 0 }}>
          {parts.length > 0 && <button className="opt" style={{ border: 'none' }} onClick={() => go({ path: parts.slice(0, -1).join('/'), file: null })}>..</button>}
          {listing.isError && <div className="error">This folder doesn't exist.</div>}
          {listing.data?.entries.length === 0 && <div className="sub" style={{ padding: 10 }}>Empty folder</div>}
          {listing.data?.entries.map((e) => (
            <button key={e.name} className={`opt ${file === e.name ? 'on' : ''}`} style={{ border: 'none', justifyContent: 'space-between' }}
              onClick={() => (e.dir ? go({ path: [...parts, e.name].join('/'), file: null }) : go({ file: e.name }))}>
              <span>{e.dir ? '▸ ' : ''}{e.name}</span>
              <span className="sub">{e.dir ? '' : bytes(e.size)}</span>
            </button>
          ))}
        </div>
        {file ? <Preview volume={volume} path={[...parts, file].join('/')} entry={listing.data?.entries.find((e) => e.name === file)} />
          : <div className="empty">Pick a file to preview it.</div>}
      </div>
    </div>
  )
}

const TEXT = /\.(md|txt|py|ts|tsx|js|json|yaml|yml|toml|sh|csv|html|css|sql)$/i

function Preview({ volume, path, entry }: { volume: Volume; path: string; entry?: S['FileEntry'] }) {
  const isText = TEXT.test(path)
  const content = useQuery({
    queryKey: ['files', 'raw', volume, path],
    enabled: isText,
    queryFn: async () => {
      const r = await fetch(`/api/files/raw?volume=${volume}&path=${encodeURIComponent(path)}`, { credentials: 'include' })
      if (!r.ok) throw new Error(String(r.status))
      return r.text()
    },
  })
  const name = path.split('/').pop()!
  return (
    <div className="preview">
      <div className="preview-head">
        <span style={{ fontWeight: 500 }}>{name}</span>
        <span className="sub">
          {bytes(entry?.size)}{entry?.modified_at && ` · ${ago(entry.modified_at)}`}
          {entry?.from_owner ? ' · from you' : entry?.last_run_id ? ` · run #${entry.last_run_id}` : ''}
        </span>
      </div>
      <div className="preview-body">
        {!isText && <div className="empty">No preview for this file type yet (images, PDF, 3D and HTML previews come in M3).</div>}
        {content.isError && <div className="error">Couldn't read the file.</div>}
        {content.data != null && (name.endsWith('.md') ? <div className="theirs" style={{ fontSize: 15 }}><Markdown>{content.data}</Markdown></div> : <pre>{content.data}</pre>)}
      </div>
    </div>
  )
}
