import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import { Link } from 'react-router'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import { routeFor } from './format'

// Agent markdown is untrusted: sanitized, no raw HTML, and only http(s), mailto and l0wagent links.
const schema = { ...defaultSchema, protocols: { ...defaultSchema.protocols, href: ['http', 'https', 'mailto', 'l0wagent'] } }

const urlTransform = (url: string) => (url.startsWith('l0wagent://') ? url : defaultUrlTransform(url))

export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      rehypePlugins={[[rehypeSanitize, schema]]}
      urlTransform={urlTransform}
      components={{
        a: ({ href, children }) => {
          const to = href ? routeFor(href) : null
          if (to) return <Link to={to}>{children}</Link>
          return <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a>
        },
      }}
    >
      {children}
    </ReactMarkdown>
  )
}
