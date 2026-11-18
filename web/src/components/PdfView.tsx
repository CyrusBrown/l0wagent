import { useEffect, useRef, useState } from 'react'
import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

/** A PDF drawn to canvases in the browser: nothing in it runs (no scripting, no annotation
 * layer, no eval), so it's safe from the main origin. Pages render as they scroll into view. */
export default function PdfView({ url, name }: { url: string; name: string }) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let dead = false
    const task = pdfjs.getDocument({ url, withCredentials: true, isEvalSupported: false, stopAtErrors: false })
    task.promise.then((d) => { if (dead) void d.destroy(); else setDoc(d) }, (e: Error) => { if (!dead) setError(e.message || 'unreadable') })
    return () => { dead = true; setDoc(null); setError(null); void task.destroy() }
  }, [url])

  if (error) return <div className="error">Couldn't read the PDF: {error}</div>
  if (!doc) return <div className="sub">Loading the PDF…</div>
  return (
    <div className="pdf-preview" aria-label={name}>
      <div className="sub">{doc.numPages} {doc.numPages === 1 ? 'page' : 'pages'}</div>
      {Array.from({ length: doc.numPages }, (_, i) => <PdfPage key={i} doc={doc} n={i + 1} />)}
    </div>
  )
}

function PdfPage({ doc, n }: { doc: PDFDocumentProxy; n: number }) {
  const box = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [visible, setVisible] = useState(false)
  const [ratio, setRatio] = useState(1.414)

  useEffect(() => {
    const el = box.current
    if (!el || typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { setVisible(true); io.disconnect() } }, { rootMargin: '400px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    if (!visible) return
    let dead = false
    let cancel: (() => void) | undefined
    ;(async () => {
      const page = await doc.getPage(n)
      const el = canvas.current
      if (dead || !el) return
      const base = page.getViewport({ scale: 1 })
      setRatio(base.height / base.width)
      const width = box.current?.clientWidth || base.width
      const dpr = window.devicePixelRatio || 1
      const viewport = page.getViewport({ scale: (width / base.width) * dpr })
      el.width = Math.floor(viewport.width)
      el.height = Math.floor(viewport.height)
      const ctx = el.getContext('2d')
      if (!ctx) return
      const task = page.render({ canvasContext: ctx, canvas: el, viewport })
      cancel = () => task.cancel()
      await task.promise
    })().catch(() => { /* a cancelled render */ })
    return () => { dead = true; cancel?.() }
  }, [visible, doc, n])

  return (
    <div ref={box} className="pdf-page" style={{ aspectRatio: `1 / ${ratio}` }}>
      <canvas ref={canvas} aria-label={`Page ${n}`} />
    </div>
  )
}
