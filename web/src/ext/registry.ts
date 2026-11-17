import type { ExtInfo } from './useExtensions'
import type { S } from '../api/client'

type Ui = NonNullable<ExtInfo['ui']>
export type Item = S['ExtensionUiItem']
export type SlotName = 'sidebar' | 'project_tabs' | 'run_widgets' | 'inbox_cards' | 'previewers' | 'thread_types'

/** Extensions whose UI is available: loaded, with a module. */
export const withUi = (exts: ExtInfo[] | undefined) => (exts ?? []).filter((e) => e.loaded && e.ui?.module)

/** Every item filling a slot, across loaded extensions, with the extension that owns it. */
export function slotItems(exts: ExtInfo[] | undefined, slot: SlotName): { ext: ExtInfo; item: Item }[] {
  return withUi(exts).flatMap((ext) => ((ext.ui as Ui)[slot] ?? []).map((item) => ({ ext, item })))
}

/** The normalised path of a page (no leading slash). */
export const pagePath = (p: string) => p.replace(/^\/+/, '')

export function pages(exts: ExtInfo[] | undefined) {
  return withUi(exts).flatMap((ext) => (ext.ui!.pages ?? []).map((page) => ({ ext, page, to: `/x/${ext.name}/${pagePath(page.path)}` })))
}

export const projectTabKey = (ext: string, id: string) => `ext:${ext}:${id}`

/** The extension previewer for a file name, if a loaded extension claims its suffix. */
export function previewerFor(exts: ExtInfo[] | undefined, path: string) {
  const name = path.toLowerCase()
  return slotItems(exts, 'previewers').find(({ item }) => (item.extensions ?? []).some((x) => name.endsWith(x.toLowerCase())))
}

/** "ext:<name>:<type>" → the thread type's UI, if loaded. */
export function threadTypeFor(exts: ExtInfo[] | undefined, type: string | undefined | null) {
  const m = /^ext:([^:]+):(.+)$/.exec(type ?? '')
  if (!m) return undefined
  return slotItems(exts, 'thread_types').find(({ ext, item }) => ext.name === m[1] && item.type === m[2])
}
