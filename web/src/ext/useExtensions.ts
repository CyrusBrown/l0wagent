import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ok, type S } from '../api/client'

export type ExtInfo = S['ExtensionInfo']

/** The installed extensions. Refetched when the live stream says the list changed. Empty while loading or on error. */
export function useExtensions() {
  return useQuery({ queryKey: ['extensions'], queryFn: () => ok(api.GET('/api/extensions')), retry: false })
}

const set = (qc: ReturnType<typeof useQueryClient>, e: ExtInfo) => {
  qc.setQueryData<ExtInfo[]>(['extensions'], (old) => (old ? old.map((x) => (x.name === e.name ? e : x)) : old))
  qc.invalidateQueries({ queryKey: ['extensions'] })
  qc.invalidateQueries({ queryKey: ['meta'] })
}

// Enabling and settings are online-only: they never go through the offline outbox.
export function useRescan() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: () => ok(api.POST('/api/extensions/rescan')), onSuccess: (list) => { qc.setQueryData(['extensions'], list); qc.invalidateQueries({ queryKey: ['meta'] }) } })
}
export function useSetExtEnabled() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { name: string; enabled: boolean }) => ok(api.PUT('/api/extensions/{name}/enabled', { params: { path: { name: a.name } }, body: { enabled: a.enabled } })),
    onSuccess: (e) => set(qc, e),
    onError: () => qc.invalidateQueries({ queryKey: ['extensions'] }),
  })
}
export function useSetExtSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { name: string; values: Record<string, unknown> }) => ok(api.PUT('/api/extensions/{name}/settings', { params: { path: { name: a.name } }, body: { values: a.values } })),
    onSuccess: (e) => set(qc, e),
  })
}
export function useResolveExtCard() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (a: { id: number; action: string; data?: Record<string, unknown> }) =>
      ok(api.POST('/api/extension-cards/{card_id}/resolve', { params: { path: { card_id: a.id } }, body: { action: a.action, data: a.data ?? {} } })),
    onSuccess: () => { for (const k of ['inbox', 'thread', 'threads', 'projects', 'status']) qc.invalidateQueries({ queryKey: [k] }) },
  })
}
