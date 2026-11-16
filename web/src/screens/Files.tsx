import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import type { S } from '../api/client'
import { problem, uploadFile, useAddToShelf, useFileDiff, useFileHistory, useListing, useProjects } from '../api/queries'
import { ago, bytes } from '../components/format'
import { CopyButton } from '../components/CopyButton'
import { kindOf, Preview, rawUrl } from '../components/Preview'
import { Caret } from '../components/Caret'
import { Select } from '../components/Select'
import { useEscape } from '../components/useEscape'

type Volume = S['Listing']['volume']
const ROOTS: [Volume | 'shared', string, string][] = [
  ['workspace', 'workspace', "The assistant's memory: notes, journal, plans. One git repo."],
  ['work', 'work', 'Project work: code, data, outputs. Your uploads are in _inbox/.'],
  ['shared', 'shared', 'Shared spaces, later (with a hub).'],
]

/** The assistant's computer, read-only (uploads aside). Layout from the mockup. */
export function Files() {
  const [params, setParams] = useSearchParams()
  const volume = (params.get('volume') === 'work' ? 'work' : 'workspace') as Volume
  const path = params.get('path') ?? ''
  const file = params.get('file')
  const rev = params.get('rev')
  const go = (p: { volume?: Volume; path?: string; file?: string | null; rev?: string | null }) => {
    const next = { volume: p.volume ?? volume, path: p.path ?? path, file: p.file === undefined ? file : p.file, rev: p.rev === undefined ? rev : p.rev }
    setParams(Object.fromEntries(Object.entries(next).filter(([, v]) => v)) as Record<string, string>)
  }
  const listing = useListing(volume, path)
  const parts = path.split('/').filter(Boolean)
  const entry = file ? listing.data?.entries.find((e) => e.name === file) : undefined
  const filePath = file ? [...parts, file].join('/') : null

  return (
    <div className="files-layout">
      <aside className="files-side">
        <div className="files-roots">
          <div className="seg wide">
            {ROOTS.map(([v, label, desc]) => (
              <button key={v} title={desc} disabled={v === 'shared'} className={v === volume ? 'on' : ''} onClick={() => v !== 'shared' && go({ volume: v, path: '', file: null, rev: null })}>{label}</button>
            ))}
          </div>
          <div className="sub" style={{ fontSize: 11.5, padding: '0 4px' }}>{ROOTS.find(([v]) => v === volume)![2]}</div>
        </div>
        <div className="files-tree">
          <Tree volume={volume} dir="" depth={0} current={path} file={file} onOpen={(p, f) => go({ path: p, file: f, rev: null })} />
        </div>
      </aside>
      <div className="files-main">
        <div className="files-bar">
          <div className="crumbs">
            <button onClick={() => go({ path: '', file: null, rev: null })}>/{volume}</button>
            {parts.map((p, i) => <span key={i}><span className="dim">/</span><button onClick={() => go({ path: parts.slice(0, i + 1).join('/'), file: null, rev: null })}>{p}</button></span>)}
            {file && <span><span className="dim">/</span><span style={{ padding: '3px 5px' }}>{file}</span></span>}
            {listing.data?.git && <span className="pill ok" style={{ marginLeft: 8 }}>git{listing.data.branch ? ` · ${listing.data.branch}` : ''}</span>}
          </div>
          {filePath ? <FileActions volume={volume} path={filePath} git={!!listing.data?.git} rev={rev} setRev={(r) => go({ rev: r })} />
            : <FolderActions volume={volume} path={path} />}
        </div>
        {filePath ? <FileView volume={volume} path={filePath} entry={entry} rev={rev} closeDiff={() => go({ rev: null })} />
          : <FolderView listing={listing.data} error={listing.error} name={parts.at(-1) ?? `/${volume}`} canUp={parts.length > 0}
              up={() => go({ path: parts.slice(0, -1).join('/'), file: null })} open={(e) => (e.dir ? go({ path: [...parts, e.name].join('/'), file: null }) : go({ file: e.name }))} />}
      </div>
    </div>
  )
}

function Tree({ volume, dir, depth, current, file, onOpen }: { volume: Volume; dir: string; depth: number; current: string; file: string | null; onOpen: (path: string, file: string | null) => void }) {
  const listing = useListing(volume, dir)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  return <>
    {listing.data?.entries.map((e) => {
      const p = dir ? `${dir}/${e.name}` : e.name
      const onPath = current === p || current.startsWith(p + '/')
      const isOpen = e.dir && (open[p] ?? onPath)
      const selected = e.dir ? current === p && !file : current === dir && file === e.name
      return (
        <div key={p}>
          <button className={`tree-row ${selected ? 'on' : ''}`} style={{ paddingLeft: 8 + depth * 14 }} title={e.symlink ? 'Symlink (not followed)' : p}
            onClick={() => {
              if (e.symlink) return
              if (e.dir) { setOpen({ ...open, [p]: !isOpen }); onOpen(p, null) } else onOpen(dir, e.name)
            }}>
            <Caret open={isOpen} hidden={!e.dir} />
            <span className="grow" style={{ fontWeight: e.dir ? 500 : 400, color: e.symlink ? 'var(--faint)' : undefined }}>{e.name}{e.symlink ? ' ↗' : ''}</span>
            {!e.dir && <span className="ext-hint">{e.name.includes('.') ? e.name.split('.').pop() : ''}</span>}
          </button>
          {isOpen && <Tree volume={volume} dir={p} depth={depth + 1} current={current} file={file} onOpen={onOpen} />}
        </div>
      )
    })}
  </>
}

function FolderView({ listing, error, name, canUp, up, open }: { listing?: S['Listing']; error: unknown; name: string; canUp: boolean; up: () => void; open: (e: S['FileEntry']) => void }) {
  if (error) return <div className="files-pane"><div className="error">{problem(error)}</div></div>
  const files = listing?.entries.filter((e) => !e.dir).length ?? 0
  const dirs = listing?.entries.filter((e) => e.dir).length ?? 0
  return (
    <div className="files-pane">
      <div className="row-start" style={{ alignItems: 'baseline', gap: 10 }}><span style={{ font: '500 22px var(--serif)' }}>{name}</span><span className="sub" style={{ fontSize: 13 }}>{dirs} folders · {files} files</span></div>
      <div className="rows">
        <div className="fs-row head"><span>Name</span><span>Size</span><span>Modified</span><span>Run</span></div>
        {canUp && <button className="fs-row" onClick={up}><span className="row-start mono" style={{ color: 'var(--muted)' }}><span className="ext-badge" />..</span><span /><span /><span /></button>}
        {listing?.entries.map((e) => (
          <button key={e.name} className="fs-row" onClick={() => !e.symlink && open(e)} disabled={e.symlink} title={e.symlink ? 'A symlink: shown, never followed' : undefined}>
            <span className="row-start" style={{ minWidth: 0 }}>
              <span className="ext-badge">{e.dir ? '▸' : e.symlink ? '↗' : (e.name.includes('.') ? e.name.split('.').pop()!.slice(0, 4) : '')}</span>
              <span className="ellipsis" style={{ fontWeight: e.dir ? 500 : 400 }}>{e.name}</span>
              {e.from_owner && <span className="pill info">from you</span>}
            </span>
            <span className="muted">{e.dir ? '' : bytes(e.size)}</span>
            <span className="muted">{e.modified_at ? ago(e.modified_at) : ''}</span>
            <span>{e.last_run_id ? <Link to={`/runs/${e.last_run_id}`} onClick={(ev) => ev.stopPropagation()}>#{e.last_run_id}</Link> : ''}</span>
          </button>
        ))}
        {listing?.entries.length === 0 && <div className="sub" style={{ padding: 32, textAlign: 'center' }}>This folder is empty.</div>}
      </div>
    </div>
  )
}

function FolderActions({ volume, path }: { volume: Volume; path: string }) {
  const projects = useProjects('active')
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const picker = useRef<HTMLInputElement>(null)
  const [target, setTarget] = useState<string | null>(null)
  useEscape(open, () => setOpen(false))
  return (
    <div className="btns" style={{ position: 'relative' }}>
      <button className="btn" onClick={() => setOpen(!open)}>Upload to a project</button>
      <input ref={picker} type="file" multiple hidden onChange={async (e) => {
        const list = Array.from(e.target.files ?? [])
        e.target.value = ''
        setStatus(`Uploading ${list.length}…`)
        try {
          for (const f of list) await uploadFile(f, target)
          setStatus(`Uploaded to /work/_inbox/${target ?? 'general'}/`)
        } catch (err) { setStatus(problem(err)) }
      }} />
      {open && <>
        <div className="menu-scrim" onClick={() => setOpen(false)} />
        <div className="menu" style={{ left: 0, right: 'auto' }}>
          <button onClick={() => { setTarget(null); setOpen(false); picker.current?.click() }}>General</button>
          {projects.data?.map((p) => <button key={p.slug} onClick={() => { setTarget(p.slug); setOpen(false); picker.current?.click() }}>{p.name}</button>)}
        </div>
      </>}
      <a className="btn" href={`/api/files/zip?volume=${volume}&path=${encodeURIComponent(path)}`} download>Download as zip</a>
      {status && <span className="sub">{status}</span>}
    </div>
  )
}

function FileActions({ volume, path, git, rev, setRev }: { volume: Volume; path: string; git: boolean; rev: string | null; setRev: (r: string | null) => void }) {
  const [menu, setMenu] = useState<'history' | 'shelf' | null>(null)
  const history = useFileHistory(volume, path, git)
  useEscape(!!menu, () => setMenu(null))
  return (
    <div className="btns" style={{ position: 'relative' }}>
      {git && <button className={`btn ${menu === 'history' ? 'pressed' : ''}`} onClick={() => setMenu(menu === 'history' ? null : 'history')}>History ▾</button>}
      <button className={`btn ${menu === 'shelf' ? 'pressed' : ''}`} onClick={() => setMenu(menu === 'shelf' ? null : 'shelf')}>Add to shelf ▾</button>
      <CopyButton src={{ kind: 'file', volume, path }} />
      <a className="btn" href={rawUrl({ kind: 'file', volume, path }, true)}>Download</a>
      {menu && <div className="menu-scrim" onClick={() => setMenu(null)} />}
      {menu === 'history' && (
        <div className="menu wide" style={{ left: 0, right: 'auto' }}>
          <div className="section-label" style={{ padding: '6px 10px' }}>Versions of {path.split('/').pop()}</div>
          {history.data?.length === 0 && <div className="sub" style={{ padding: '6px 10px' }}>Not committed yet.</div>}
          {history.data?.map((h) => (
            <button key={h.rev} className={`hist-row ${rev === h.rev ? 'on' : ''}`} onClick={() => { setRev(h.rev); setMenu(null) }}>
              <span>{h.run_id ? `run #${h.run_id}` : h.rev.slice(0, 7)}</span>
              <span className="muted ellipsis">{ago(h.at)} · {h.message}</span>
              <span className="mono" style={{ fontSize: 12 }}><span style={{ color: 'var(--okInk)' }}>+{h.added}</span> <span style={{ color: 'var(--badInk)' }}>−{h.removed}</span></span>
            </button>
          ))}
          {!!history.data?.length && <div className="sub" style={{ padding: '8px 10px 4px', borderTop: '1px solid var(--lineSoft)' }}>Pick a version to see its diff with the previous one.</div>}
        </div>
      )}
      {menu === 'shelf' && <ShelfMenu volume={volume} path={path} done={() => setMenu(null)} />}
    </div>
  )
}

function ShelfMenu({ volume, path, done }: { volume: Volume; path: string; done: () => void }) {
  const projects = useProjects('active')
  const add = useAddToShelf()
  const [project, setProject] = useState<string>('')
  const [mode, setMode] = useState<'live' | 'snapshot'>('live')
  const [keep, setKeep] = useState(0)
  useEffect(() => { if (!project && projects.data?.[0]) setProject(projects.data[0].slug) }, [project, projects.data])
  return (
    <form className="menu wide stack" style={{ left: 'auto', right: 0, padding: 12, gap: 10 }} onSubmit={(e) => {
      e.preventDefault()
      add.mutate({ project, volume, path, label: null, mode, keep_versions: mode === 'live' ? keep : 0 }, { onSuccess: done })
    }}>
      <div className="section-label">Add to a project's shelf</div>
      {projects.data?.length === 0 ? <div className="sub">Create a project first.</div> : (
        <Select variant="input" value={project} label="Project"
          options={projects.data?.map((p) => ({ value: p.slug, label: p.name })) ?? []}
          onChange={setProject} />
      )}
      <div className="seg">
        <button type="button" className={mode === 'live' ? 'on' : ''} onClick={() => setMode('live')}>Live</button>
        <button type="button" className={mode === 'snapshot' ? 'on' : ''} onClick={() => setMode('snapshot')}>Snapshot</button>
      </div>
      <div className="sub">{mode === 'live' ? 'Always the current file.' : 'A copy, as it is now.'}</div>
      {mode === 'live' && <label className="row-start sub">Keep <input className="input" type="number" min={0} max={100} style={{ width: 64 }} value={keep} onChange={(e) => setKeep(Number(e.target.value))} /> old versions</label>}
      {add.error && <div className="error">{problem(add.error)}</div>}
      <div><button className="btn primary small" disabled={!project || add.isPending}>Add</button></div>
    </form>
  )
}

function FileView({ volume, path, entry, rev, closeDiff }: { volume: Volume; path: string; entry?: S['FileEntry']; rev: string | null; closeDiff: () => void }) {
  const [raw, setRaw] = useState(false)
  const diff = useFileDiff(volume, path, rev)
  const name = path.split('/').pop()!
  const textual = ['markdown', 'csv', 'json', 'notebook'].includes(kindOf(path))
  const meta = [bytes(entry?.size), entry?.modified_at && `modified ${ago(entry.modified_at)}`, entry?.last_run_id && `last changed in run #${entry.last_run_id}`, entry?.from_owner && 'from you'].filter(Boolean).join(' · ')
  return (
    <div className="files-pane" style={{ maxWidth: 920 }}>
      <div className="row-between" style={{ alignItems: 'flex-end' }}>
        <div className="stack" style={{ gap: 4 }}><span className="mono" style={{ font: '500 20px var(--mono)' }}>{name}</span><span className="sub" style={{ fontSize: 13 }}>{meta}</span></div>
        {textual && !rev && <div className="seg"><button className={!raw ? 'on' : ''} onClick={() => setRaw(false)}>Preview</button><button className={raw ? 'on' : ''} onClick={() => setRaw(true)}>Raw</button></div>}
      </div>
      {rev ? (
        <div className="stack">
          <div className="row-between">
            <span className="sub" style={{ fontSize: 13 }}>{diff.data ? <>{diff.data.run_id ? <Link to={`/runs/${diff.data.run_id}`}>run #{diff.data.run_id}</Link> : rev.slice(0, 7)} · {ago(diff.data.at)} · {diff.data.message.split('\n')[0]}</> : 'Loading…'}</span>
            <button className="btn small" onClick={closeDiff}>Back to the file</button>
          </div>
          {diff.error && <div className="error">{problem(diff.error)}</div>}
          {diff.data && <pre className="code-preview diff">{diff.data.patch.split('\n').map((l, i) => <div key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : ''}>{l || ' '}</div>)}</pre>}
        </div>
      ) : raw ? <Preview src={{ kind: 'file', volume, path }} size={entry?.size} stamp={entry?.modified_at ?? undefined} as="code" />
        : <Preview src={{ kind: 'file', volume, path }} size={entry?.size} stamp={entry?.modified_at ?? undefined} />}
    </div>
  )
}
