import { useState } from 'react'
import { Link } from 'react-router'
import type { S } from '../api/client'
import { problem, uploadFile, useRemoveFromShelf, useShelf, useShelfVersions } from '../api/queries'
import { ago, bytes } from './format'
import { Select, type Option } from './Select'
import { Preview, rawUrl } from './Preview'
import { CopyButton } from './CopyButton'

const ext = (p: string) => (p.includes('.') ? p.split('.').pop()!.toUpperCase().slice(0, 4) : '')

/** A project's shelf: items (live / snapshot, from you / the assistant) and a preview with
 * the version picker. `compact` is the thread's side panel. */
export function ShelfView({ project, compact, onLink }: { project: string; compact?: boolean; onLink?: (item: S['ShelfItem']) => void }) {
  const shelf = useShelf(project)
  const [sel, setSel] = useState<number | null>(null)
  const items = shelf.data ?? []
  const item = items.find((i) => i.id === sel) ?? items[0]
  return (
    <div className={compact ? 'shelf compact' : 'shelf'}>
      <div className="stack" style={{ gap: 12 }}>
        {!compact && <DropZone project={project} />}
        <div className="rows">
          {!compact && <div className="shelf-row head"><span>Label</span><span>Mode</span><span>Updated</span></div>}
          {items.length === 0 && <div className="sub" style={{ padding: 16 }}>Nothing on the shelf yet. The assistant shelves deliverables here (<code>assistant shelf add</code>); you can add any file from Files.</div>}
          {items.map((i) => (
            <button key={i.id} className={`shelf-row ${item?.id === i.id ? 'on' : ''}`} onClick={() => setSel(i.id)}>
              <span className="row-start" style={{ minWidth: 0 }}>
                <span className="ext-badge">{ext(i.path)}</span>
                <span className="stack" style={{ gap: 1, minWidth: 0 }}>
                  <span className="ellipsis" style={{ fontWeight: 500 }}>{i.label}</span>
                  {!compact && <span className="ellipsis mono" style={{ fontSize: 11.5, color: 'var(--faint2)' }}>{i.added_by === 'owner' ? 'from you' : 'assistant'} · /{i.volume}/{i.path}</span>}
                </span>
              </span>
              <span className={`mode ${i.missing ? 'missing' : i.mode}`}>{i.missing ? 'missing' : i.mode}</span>
              {!compact && <span className="muted">{ago(i.updated_at)}</span>}
            </button>
          ))}
        </div>
      </div>
      {item && <ShelfPreview key={item.id} item={item} compact={compact} onLink={onLink} />}
    </div>
  )
}

function ShelfPreview({ item, compact, onLink }: { item: S['ShelfItem']; compact?: boolean; onLink?: (item: S['ShelfItem']) => void }) {
  const versions = useShelfVersions(item.versions > 0 ? item.id : null)
  const [version, setVersion] = useState<number | null>(null)
  const remove = useRemoveFromShelf()
  const v = versions.data?.find((x) => x.n === version)
  const src = { kind: 'shelf' as const, id: item.id, path: item.path, version: item.mode === 'snapshot' ? null : version }
  return (
    <div className="shelf-preview">
      <div className="row-between shelf-preview-head">
        <div className="stack" style={{ gap: 2, minWidth: 0 }}>
          <span className="ellipsis" style={{ fontWeight: 500, fontSize: 14 }}>{item.label}</span>
          <span className="sub">{item.mode === 'live' ? 'Live' : 'Snapshot'} · {item.added_by === 'owner' ? 'from you' : 'the assistant'}{item.run_id ? <> · <Link to={`/runs/${item.run_id}`}>run #{item.run_id}</Link></> : ''} · {bytes(v?.size ?? item.size)}</span>
        </div>
        {item.mode === 'live' && !!versions.data?.length && (
          <Select variant="input" className="version-pick" value={version ?? ''} label="Version"
            options={[{ value: '', label: 'Current' }, ...versions.data.map((x) => ({ value: x.n, label: `v${x.n} · ${ago(x.created_at)}` }))] as Option<number | ''>[]}
            onChange={(v) => setVersion(v === '' ? null : v)} />
        )}
      </div>
      <div className="shelf-preview-body">
        {item.missing && version == null ? <div className="empty">The file is gone from /{item.volume}/{item.path}.{item.versions ? ' Kept versions are still here.' : ''}</div>
          : <Preview src={src} size={v?.size ?? item.size} stamp={`${item.updated_at}-${version}`} compact={compact} />}
      </div>
      <div className="btns" style={{ padding: compact ? '12px 16px' : '12px 14px', position: 'relative' }}>
        {onLink && <button className="btn small" onClick={() => onLink(item)}>Link in reply</button>}
        <CopyButton src={src} volume={item.volume} className="btn small" />
        <a className="btn small" href={rawUrl(src, true)}>Download</a>
        {compact ? <Link className="btn small" to={`/p/${item.project}?tab=files`}>Open in project</Link>
          : <Link className="btn small" to={`/files?volume=${item.volume}&path=${encodeURIComponent(item.path.split('/').slice(0, -1).join('/'))}&file=${encodeURIComponent(item.path.split('/').pop()!)}`}>Show in Files</Link>}
        {!compact && <button className="btn small danger" style={{ marginLeft: 'auto' }} disabled={remove.isPending} onClick={() => remove.mutate(item.id)}>Remove from shelf</button>}
      </div>
    </div>
  )
}

function DropZone({ project }: { project: string }) {
  const [state, setState] = useState<string | null>(null)
  const [over, setOver] = useState(false)
  const drop = async (files: FileList) => {
    setState(`Uploading ${files.length}…`)
    try {
      for (const f of Array.from(files)) await uploadFile(f, project)
      setState(`Added to /work/_inbox/${project}/ (the assistant sees it there)`)
    } catch (e) { setState(problem(e)) }
  }
  return (
    <label className={`drop-zone ${over ? 'over' : ''}`} onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); if (e.dataTransfer.files.length) void drop(e.dataTransfer.files) }}>
      <input type="file" multiple hidden onChange={(e) => { if (e.target.files?.length) void drop(e.target.files); e.target.value = '' }} />
      {state ?? 'Drop files to add them to the project inbox folder'}
    </label>
  )
}
