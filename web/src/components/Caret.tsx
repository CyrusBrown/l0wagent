/** The mockup's disclosure triangle (▸ / ▾), drawn so it doesn't depend on a font having the glyph. */
export function Caret({ open, hidden }: { open: boolean; hidden?: boolean }) {
  return (
    <span className={`caret ${open ? 'open' : ''}`} aria-hidden="true" style={hidden ? { visibility: 'hidden' } : undefined}>
      <svg width="8" height="8" viewBox="0 0 8 8"><path d="M2 1 6.5 4 2 7z" fill="currentColor" /></svg>
    </span>
  )
}
