import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import { Link } from 'react-router'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import { CodeBox } from './CopyCode'
import { routeFor } from './format'

// Agent markdown is untrusted: sanitized, no raw HTML, and only http(s), mailto and l0wagent links.
const schema = { ...defaultSchema, protocols: { ...defaultSchema.protocols, href: ['http', 'https', 'mailto', 'l0wagent'] } }

const urlTransform = (url: string) => (url.startsWith('l0wagent://') ? url : defaultUrlTransform(url))

/** A relative link in a note (`knowledge/user.md`, `../NOW.md#next`) → the file in the browser. */
export function resolveRelative(href: string, base: { volume: string; dir: string }): string | null {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('#') || href.startsWith('//')) return null
  const clean = href.split('#')[0].split('?')[0]
  if (!clean) return null
  const parts = clean.startsWith('/') ? [] : base.dir.split('/').filter(Boolean)
  for (const p of clean.split('/')) {
    if (!p || p === '.') continue
    if (p === '..') { if (!parts.length) return null; parts.pop() } else parts.push(decodeURIComponent(p))
  }
  const file = parts.pop()
  if (!file) return null
  return `/files?volume=${encodeURIComponent(base.volume)}&path=${encodeURIComponent(parts.join('/'))}&file=${encodeURIComponent(file)}`
}

// A bare `l0wagent://thread/20` in the text (models often write them without link syntax) becomes a link named
// for what it points at ("thread #20"). Code and existing links are left alone.
const BARE_REF = /l0wagent:\/\/(?:thread|project|run|file|chat)\/[^\s<>()[\]`'"]+/g

export function refLabel(url: string): string {
  const m = /^l0wagent:\/\/(thread|project|run|file|chat)\/(.+)$/.exec(url)
  if (!m) return url
  const [, kind, rest] = m
  const id = rest.replace(/#.*$/, '')
  if (kind === 'thread' || kind === 'run') return `${kind} #${id}`
  if (kind === 'file') return decodeURIComponent(id.split('/').pop() || id)
  return `${kind} ${decodeURIComponent(id)}`
}

type MdNode = { type: string; value?: string; url?: string; children?: MdNode[] }

export function remarkBareRefs() {
  const split = (text: string): MdNode[] => {
    const out: MdNode[] = []
    let last = 0
    for (const m of text.matchAll(BARE_REF)) {
      const url = m[0].replace(/[.,;:!?]+$/, '')
      const at = m.index ?? 0
      if (at > last) out.push({ type: 'text', value: text.slice(last, at) })
      out.push({ type: 'link', url, children: [{ type: 'text', value: refLabel(url) }] })
      last = at + url.length
    }
    if (last < text.length) out.push({ type: 'text', value: text.slice(last) })
    return out
  }
  const walk = (node: MdNode) => {
    if (!node.children || node.type === 'link' || node.type === 'linkReference') return
    node.children = node.children.flatMap((c) => (c.type === 'text' && c.value?.includes('l0wagent://') ? split(c.value) : (walk(c), [c])))
  }
  return (tree: MdNode) => walk(tree)
}

export function Markdown({ children, fileBase }: { children: string; fileBase?: { volume: string; dir: string } }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkBareRefs]}
      rehypePlugins={[[rehypeSanitize, schema]]}
      urlTransform={urlTransform}
      components={{
        a: ({ href, children }) => {
          const to = href ? routeFor(href) ?? (fileBase ? resolveRelative(href, fileBase) : null) : null
          if (to) return <Link to={to}>{children}</Link>
          return <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a>
        },
        pre: ({ children, node }) => {
          const code = node?.children?.[0]
          const cls = code && 'properties' in code ? code.properties?.className : undefined
          const lang = (Array.isArray(cls) ? cls : []).map(String).find((c) => c.startsWith('language-'))?.slice(9)
          return <CodeBox label={lang || 'Code'}><pre>{children}</pre></CodeBox>
        },
      }}
    >
      {children}
    </ReactMarkdown>
  )
}
