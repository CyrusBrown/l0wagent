import { Component, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { createHost, type L0wHost } from './host'
import { loadModule } from './loader'
import type { ExtInfo } from './useExtensions'

type ExtEl = HTMLElement & { l0w?: L0wHost; context?: unknown }

/** Creates the custom element, sets `l0w` and `context` before it is attached (so connectedCallback can use
 * them), and removes it on unmount. A changed `context` is assigned again. */
export function ExtElement({ element, l0w, context }: { element: string; l0w: L0wHost; context: unknown }) {
  const box = useRef<HTMLDivElement>(null)
  const el = useRef<ExtEl | null>(null)
  const ctx = useRef(context)
  ctx.current = context
  useEffect(() => {
    const e = document.createElement(element) as ExtEl
    e.l0w = l0w
    e.context = ctx.current
    box.current!.appendChild(e)
    el.current = e
    return () => { e.remove(); el.current = null }
  }, [element, l0w])
  useEffect(() => { if (el.current && el.current.context !== context) el.current.context = context }, [context])
  return <div ref={box} className="ext-slot" />
}

/** A slot's error never takes the app down. */
export class ExtBoundary extends Component<{ ext: string; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null }
  static getDerivedStateFromError(e: unknown) { return { error: e instanceof Error ? e.message : String(e) } }
  render() { return this.state.error ? <ExtProblem ext={this.props.ext} text={this.state.error} /> : this.props.children }
}

export function ExtProblem({ ext, text }: { ext: string; text: string }) {
  return <div className="error ext-problem" role="alert">The “{ext}” extension couldn't show this: {text}</div>
}

/** Keep the previous context object while its entries are unchanged, so callers can pass literals. */
function useStable<T extends Record<string, unknown>>(v: T): T {
  const prev = useRef(v)
  const a = Object.keys(v), b = Object.keys(prev.current)
  if (a.length !== b.length || a.some((k) => v[k] !== prev.current[k])) prev.current = v
  return prev.current
}

/** One slot of an extension: loads its module (once), then renders the element. */
export function ExtSlot({ ext, element, context }: { ext: ExtInfo; element: string; context: Record<string, unknown> }) {
  const nav = useNavigate()
  const host = useMemo(() => createHost(ext.name, (to) => { void nav(to) }), [ext.name, nav])
  const url = ext.ui?.module ?? null
  const [state, setState] = useState<{ key: string; error: string | null } | null>(null)
  const key = `${url}|${ext.version}|${element}`
  const stable = useStable(context)
  useEffect(() => {
    if (!url) return
    let live = true
    loadModule(url, ext.version).then(
      () => { if (live) setState({ key, error: customElements.get(element) ? null : `the element <${element}> is not defined by its module` }) },
      (e: unknown) => { if (live) setState({ key, error: e instanceof Error ? e.message : String(e) }) },
    )
    return () => { live = false }
  }, [url, ext.version, element, key])
  if (!url) return null
  if (state?.key !== key) return <div className="sub">Loading…</div>
  if (state.error) return <ExtProblem ext={ext.name} text={state.error} />
  return <ExtBoundary ext={ext.name}><ExtElement element={element} l0w={host} context={stable} /></ExtBoundary>
}
