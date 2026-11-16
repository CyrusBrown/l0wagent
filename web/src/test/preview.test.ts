import { describe, expect, it } from 'vitest'
import { resolveRelative } from '../components/Markdown'
import { kindOf, parseCsv, rawUrl } from '../components/Preview'

describe('previews', () => {
  it('picks a previewer by name', () => {
    expect(kindOf('notes/NOW.md')).toBe('markdown')
    expect(kindOf('a/page.html')).toBe('html')
    expect(kindOf('logo.svg')).toBe('image') // as <img> from the files origin: no script runs
    expect(kindOf('part.STEP')).toBe('model')
    expect(kindOf('run.py')).toBe('code')
    expect(kindOf('Makefile')).toBe('code')
    expect(kindOf('blob.bin')).toBe('binary')
  })
  it('parses CSV with quotes', () => {
    expect(parseCsv('a,b\n1,"x, ""y"""\n2,z\n')).toEqual([['a', 'b'], ['1', 'x, "y"'], ['2', 'z']])
    expect(parseCsv('a\tb\r\n1\t2', '\t')).toEqual([['a', 'b'], ['1', '2']])
  })
  it('links between notes resolve inside the volume', () => {
    const base = { volume: 'workspace', dir: 'knowledge' }
    expect(resolveRelative('user.md', base)).toBe('/files?volume=workspace&path=knowledge&file=user.md')
    expect(resolveRelative('../NOW.md#next', base)).toBe('/files?volume=workspace&path=&file=NOW.md')
    expect(resolveRelative('../../etc/passwd', base)).toBeNull()
    expect(resolveRelative('https://example.com', base)).toBeNull()
    expect(resolveRelative('#heading', base)).toBeNull()
  })
  it('raw bytes come from the main origin; downloads say so', () => {
    expect(rawUrl({ kind: 'file', volume: 'work', path: 'a b/c.csv' }, true)).toBe('/api/files/raw?volume=work&path=a%20b%2Fc.csv&download=true')
    expect(rawUrl({ kind: 'shelf', id: 3, path: 'x', version: 2 })).toBe('/api/shelf/3/raw?version=2')
  })
})
