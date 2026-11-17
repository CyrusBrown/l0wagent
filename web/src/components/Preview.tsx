import { useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useMemo, useState } from 'react'
import { api, ok, type S } from '../api/client'
import { bytes } from './format'
import { ExtSlot } from '../ext/ExtElement'
import { previewerFor } from '../ext/registry'
import { useExtensions } from '../ext/useExtensions'
import { Markdown } from './Markdown'

const Model3D = lazy(() => import('./Model3D'))

/** What a preview shows: a file in the agent's storage, or a shelf item (optionally a kept version). */
export type Source =
  | { kind: 'file'; volume: S['Listing']['volume']; path: string }
  | { kind: 'shelf'; id: number; path: string; version?: number | null }

export type PreviewKind = 'markdown' | 'code' | 'csv' | 'json' | 'notebook' | 'image' | 'audio' | 'video' | 'pdf' | 'html' | 'model' | 'binary'

const EXT: Record<string, PreviewKind> = {
  md: 'markdown', markdown: 'markdown', csv: 'csv', tsv: 'csv', json: 'json', ipynb: 'notebook',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', avif: 'image', bmp: 'image', ico: 'image', svg: 'image',
  mp3: 'audio', wav: 'audio', ogg: 'audio', m4a: 'audio', flac: 'audio', opus: 'audio',
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video', pdf: 'pdf', html: 'html', htm: 'html',
  stl: 'model', '3mf': 'model', step: 'model', stp: 'model',
}
const TEXTISH = /\.(txt|log|py|ts|tsx|js|jsx|mjs|css|scss|sh|bash|zsh|toml|ya?ml|ini|cfg|conf|env|sql|rs|go|rb|java|kt|c|h|cc|cpp|hpp|swift|lua|r|jl|tex|xml|gcode|scad|dockerfile|gitignore|jsonl|ndjson)$|(^|\/)(Dockerfile|Makefile|README|LICENSE|crontab)$/i
const TEXT_MAX = 2 * 1024 * 1024

export function kindOf(path: string): PreviewKind {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return EXT[ext] ?? (TEXTISH.test(path) ? 'code' : 'binary')
}

/** Bytes on the main origin (never rendered there): for text and client-side parsers. */
export function rawUrl(src: Source, download = false): string {
  const dl = download ? '&download=true' : ''
  if (src.kind === 'shelf') return `/api/shelf/${src.id}/raw?${src.version != null ? `version=${src.version}` : ''}${dl}`
  return `/api/files/raw?volume=${src.volume}&path=${encodeURIComponent(src.path)}${dl}`
}

/** A signed link on the files origin: for frames and media elements. */
function usePreviewUrl(src: Source, enabled: boolean) {
  return useQuery({
    queryKey: ['files', 'preview-url', src],
    enabled,
    staleTime: 30 * 60_000,
    queryFn: () => src.kind === 'shelf'
      ? ok(api.GET('/api/shelf/{item_id}/preview-url', { params: { path: { item_id: src.id }, query: { version: src.version ?? undefined } } }))
      : ok(api.GET('/api/files/preview-url', { params: { query: { volume: src.volume, path: src.path } } })),
  })
}

function useText(src: Source, enabled: boolean, version?: string | number) {
  return useQuery({
    queryKey: ['files', 'text', src, version],
    enabled,
    queryFn: async () => {
      const r = await fetch(rawUrl(src), { credentials: 'include' })
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `HTTP ${r.status}`)
      return r.text()
    },
  })
}

/** A file's preview. `size` (if known) keeps big text files from being fetched; `stamp`
 * (e.g. the modified time) refetches when the file changes. */
export function Preview(props: { src: Source; size?: number | null; stamp?: string | number; compact?: boolean; as?: PreviewKind }) {
  const exts = useExtensions()
  const [builtin, setBuiltin] = useState(false)
  const hit = props.as ? undefined : previewerFor(exts.data, props.src.path)
  if (!hit) return <BuiltinPreview {...props} />
  const hasBuiltin = kindOf(props.src.path) !== 'binary'
  const title = hit.item.title ?? hit.ext.title
  const context = { volume: props.src.kind === 'file' ? props.src.volume : null, path: props.src.path, url: rawUrl(props.src) }
  return (
    <div className="stack" style={{ gap: 8 }}>
      {hasBuiltin && <div><div className="seg"><button className={!builtin ? 'on' : ''} onClick={() => setBuiltin(false)}>{title}</button><button className={builtin ? 'on' : ''} onClick={() => setBuiltin(true)}>Built-in view</button></div></div>}
      {builtin && hasBuiltin ? <BuiltinPreview {...props} /> : <ExtSlot ext={hit.ext} element={hit.item.element} context={context} />}
    </div>
  )
}

function BuiltinPreview({ src, size, stamp, compact, as }: { src: Source; size?: number | null; stamp?: string | number; compact?: boolean; as?: PreviewKind }) {
  const kind = as ?? kindOf(src.path)
  const textual = ['markdown', 'code', 'csv', 'json', 'notebook'].includes(kind)
  const tooBig = textual && size != null && size > TEXT_MAX
  const text = useText(src, textual && !tooBig, stamp)
  const framed = ['image', 'audio', 'video', 'pdf', 'html'].includes(kind)
  const link = usePreviewUrl(src, framed)
  const name = src.path.split('/').pop() ?? ''

  if (tooBig) return <Binary note={`${bytes(size)}: too big to preview here. Download it instead.`} />
  if (textual) {
    if (text.isError) return <div className="error">Couldn't read the file: {(text.error as Error).message}</div>
    if (text.data == null) return <div className="sub">Loading…</div>
    if (kind === 'markdown') {
      const base = src.kind === 'file' ? { volume: src.volume, dir: src.path.split('/').slice(0, -1).join('/') } : undefined
      return <div className={`md-preview ${compact ? 'compact' : ''}`}><Markdown fileBase={base}>{text.data}</Markdown></div>
    }
    if (kind === 'csv') return <CsvTable text={text.data} tab={name.endsWith('.tsv')} />
    if (kind === 'json') return <pre className="code-preview">{pretty(text.data)}</pre>
    if (kind === 'notebook') return <Notebook text={text.data} />
    return <CodeView text={text.data} />
  }
  if (framed) {
    if (link.isError) return <div className="error">Couldn't open a preview link.</div>
    const url = link.data?.url
    if (!url) return <div className="sub">Loading…</div>
    if (kind === 'image') return <div className="media-preview"><img src={url} alt={name} /></div>
    if (kind === 'audio') return <audio className="audio-preview" controls src={url} />
    if (kind === 'video') return <video className="video-preview" controls src={url} />
    if (kind === 'pdf') return <iframe className="frame-preview" title={name} src={url} />
    return (
      <div className="stack" style={{ gap: 6 }}>
        <div className="sub row-start"><span className="pill ok">sandboxed</span>Agent-made page, shown on a separate origin. <a href={url} target="_blank" rel="noopener noreferrer">Open in new tab</a></div>
        <iframe className="frame-preview" title={name} src={url} sandbox="allow-scripts allow-forms allow-popups allow-modals" referrerPolicy="no-referrer" />
      </div>
    )
  }
  if (kind === 'model') return <Suspense fallback={<div className="sub">Loading the 3D viewer…</div>}><Model3D url={rawUrl(src)} name={name} /></Suspense>
  return <Binary note={`${bytes(size)} · no preview for this type. Download it to open it.`} />
}

function Binary({ note }: { note: string }) {
  return <div className="binary-preview"><span className="mono" style={{ fontWeight: 500, fontSize: 14 }}>Binary data</span><span className="sub" style={{ fontSize: 13 }}>{note}</span></div>
}

function pretty(text: string) {
  try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return text }
}

function CodeView({ text }: { text: string }) {
  const lines = text.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return (
    <pre className="code-preview numbered">
      {lines.map((l, i) => <div key={i}><span className="ln">{i + 1}</span>{l || ' '}</div>)}
    </pre>
  )
}

/** A small CSV parser (quotes, escaped quotes, newlines in quotes). Shows the first 500 rows. */
export function parseCsv(text: string, sep = ','): string[][] {
  const rows: string[][] = []
  let row: string[] = [], cell = '', quoted = false
  for (let i = 0; i < text.length && rows.length <= 500; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++ } else if (ch === '"') quoted = false
      else cell += ch
    } else if (ch === '"' && cell === '') quoted = true
    else if (ch === sep) { row.push(cell); cell = '' }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(cell); rows.push(row); row = []; cell = ''
    } else cell += ch
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row) }
  return rows
}

function CsvTable({ text, tab }: { text: string; tab: boolean }) {
  const rows = useMemo(() => parseCsv(text, tab ? '\t' : ','), [text, tab])
  if (!rows.length) return <div className="sub">Empty file.</div>
  const [head, ...body] = rows
  return (
    <div className="csv-wrap">
      <table className="table csv">
        <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
        <tbody>{body.slice(0, 500).map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
      {rows.length > 500 && <div className="sub" style={{ padding: 8 }}>First 500 rows.</div>}
    </div>
  )
}

type Cell = { cell_type: string; source: string | string[]; outputs?: { output_type: string; text?: string | string[]; data?: Record<string, string | string[]>; ename?: string; evalue?: string }[] }
const join = (v: string | string[] | undefined) => (Array.isArray(v) ? v.join('') : v ?? '')

/** Notebook cells: markdown, code and outputs (text, images; HTML outputs shown as text). */
function Notebook({ text }: { text: string }) {
  const nb = useMemo(() => { try { return JSON.parse(text) as { cells: Cell[] } } catch { return null } }, [text])
  if (!nb?.cells) return <div className="error">Not a readable notebook.</div>
  return (
    <div className="notebook">
      {nb.cells.map((c, i) => c.cell_type === 'markdown' ? <div key={i} className="md-preview"><Markdown>{join(c.source)}</Markdown></div> : (
        <div key={i} className="nb-cell">
          <pre className="code-preview">{join(c.source)}</pre>
          {c.outputs?.map((o, j) => {
            const png = o.data?.['image/png']
            if (png) return <img key={j} className="nb-img" alt="output" src={`data:image/png;base64,${join(png).replace(/\s/g, '')}`} />
            const jpeg = o.data?.['image/jpeg']
            if (jpeg) return <img key={j} className="nb-img" alt="output" src={`data:image/jpeg;base64,${join(jpeg).replace(/\s/g, '')}`} />
            const out = o.output_type === 'error' ? `${o.ename}: ${o.evalue}` : join(o.text) || join(o.data?.['text/plain'])
            return out ? <pre key={j} className={`nb-out ${o.output_type === 'error' ? 'err' : ''}`}>{out}</pre> : null
          })}
        </div>
      ))}
    </div>
  )
}
