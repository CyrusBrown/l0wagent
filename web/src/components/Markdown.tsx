import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import { Link } from 'react-router'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
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

export function Markdown({ children, fileBase }: { children: string; fileBase?: { volume: string; dir: string } }) {
  return (
    <ReactMarkdown
      rehypePlugins={[[rehypeSanitize, schema]]}
      urlTransform={urlTransform}
      components={{
        a: ({ href, children }) => {
          const to = href ? routeFor(href) ?? (fileBase ? resolveRelative(href, fileBase) : null) : null
          if (to) return <Link to={to}>{children}</Link>
          return <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a>
        },
      }}
    >
      {children}
    </ReactMarkdown>
  )
}
