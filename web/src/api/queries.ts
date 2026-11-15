import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, newKey, ok, type S } from './client'

// Query keys start with the entity name so live events can invalidate by prefix (see live.ts).

export const useMeta = () => useQuery({ queryKey: ['meta'], queryFn: () => ok(api.GET('/api/meta')), staleTime: Infinity })
export const useMe = () => useQuery({ queryKey: ['me'], queryFn: () => ok(api.GET('/api/auth/me')), retry: false })
export const useStatus = () => useQuery({ queryKey: ['status'], queryFn: () => ok(api.GET('/api/status')) })

export const useProjects = (status?: S['Project']['status']) =>
  useQuery({ queryKey: ['projects', status ?? 'all'], queryFn: () => ok(api.GET('/api/projects', { params: { query: { status } } })) })
export const useProject = (slug: string) =>
  useQuery({ queryKey: ['projects', 'one', slug], queryFn: () => ok(api.GET('/api/projects/{slug}', { params: { path: { slug } } })) })

export const useThreads = (q: { project?: string; general?: boolean; archived?: boolean } = {}) =>
  useQuery({ queryKey: ['threads', q], queryFn: () => ok(api.GET('/api/threads', { params: { query: q } })) })
export const useThread = (id: number) =>
  useQuery({ queryKey: ['thread', id], queryFn: () => ok(api.GET('/api/threads/{thread_id}', { params: { path: { thread_id: id } } })) })
export const useThreadItems = (id: number) =>
  useQuery({ queryKey: ['thread', id, 'items'], queryFn: () => ok(api.GET('/api/threads/{thread_id}/items', { params: { path: { thread_id: id }, query: { limit: 200 } } })) })
export const useDelivery = (id: number) =>
  useQuery({ queryKey: ['thread', id, 'delivery'], queryFn: () => ok(api.GET('/api/threads/{thread_id}/delivery', { params: { path: { thread_id: id } } })) })

export const useInbox = (view: 'waiting' | 'history', kind?: S['InboxEntry']['kind'], q?: string) =>
  useQuery({ queryKey: ['inbox', view, kind ?? 'all', q ?? ''], queryFn: () => ok(api.GET('/api/inbox', { params: { query: { view, kind, q: q || undefined } } })) })
export const useInboxCounts = () => useQuery({ queryKey: ['inbox', 'counts'], queryFn: () => ok(api.GET('/api/inbox/counts')) })
export const useClaims = () => useQuery({ queryKey: ['inbox', 'claims'], queryFn: () => ok(api.GET('/api/claims', { params: { query: { active: true } } })) })

export const useRuns = (kind?: S['Run']['kind'], project?: string) =>
  useQuery({ queryKey: ['runs', kind ?? 'all', project ?? ''], queryFn: () => ok(api.GET('/api/runs', { params: { query: { kind, project } } })) })
export const useRun = (id: number) =>
  useQuery({ queryKey: ['run', id], queryFn: () => ok(api.GET('/api/runs/{run_id}', { params: { path: { run_id: id } } })) })
export const useTimeline = (id: number, view: 'simple' | 'detailed') =>
  useQuery({ queryKey: ['run', id, 'timeline', view], queryFn: () => ok(api.GET('/api/runs/{run_id}/timeline', { params: { path: { run_id: id }, query: { view } } })) })
export const useJobs = () => useQuery({ queryKey: ['jobs'], queryFn: () => ok(api.GET('/api/jobs')) })

export const useListing = (volume: S['Listing']['volume'], path: string) =>
  useQuery({ queryKey: ['files', volume, path], queryFn: () => ok(api.GET('/api/files/list', { params: { query: { volume, path } } })) })
export const useShelf = (slug: string) =>
  useQuery({ queryKey: ['shelf', slug], queryFn: () => ok(api.GET('/api/projects/{slug}/shelf', { params: { path: { slug } } })) })

export const useSearch = (q: string) =>
  useQuery({ queryKey: ['search', q], queryFn: () => ok(api.GET('/api/search', { params: { query: { q } } })), enabled: q.trim().length > 0 })

/** Mutations invalidate broadly; the live stream also invalidates, so this is belt and braces. */
function useInvalidating<A, R>(fn: (a: A) => Promise<R>, keys: string[][]) {
  const qc = useQueryClient()
  return useMutation({ mutationFn: fn, onSuccess: () => keys.forEach((queryKey) => qc.invalidateQueries({ queryKey })) })
}

const cardKeys = [['inbox'], ['thread'], ['threads'], ['projects'], ['status']]

export const usePostMessage = (threadId: number) =>
  useInvalidating((m: string | { body: string; upload_ids?: number[] }) => {
    const { body, upload_ids = [] } = typeof m === 'string' ? { body: m } : m
    return ok(api.POST('/api/threads/{thread_id}/messages', { params: { path: { thread_id: threadId } }, body: { body, upload_ids, idempotency_key: newKey() } }))
  }, [['thread'], ['threads']])

export const useReplyNow = (threadId: number) =>
  useInvalidating(() => ok(api.POST('/api/threads/{thread_id}/reply-now', { params: { path: { thread_id: threadId } } })), [['status'], ['runs'], ['thread']])

export const useAnswer = () =>
  useInvalidating((a: { id: number; value: unknown; note?: string }) =>
    ok(api.POST('/api/questions/{qid}/answer', { params: { path: { qid: a.id } }, body: { value: a.value, note: a.note || null, idempotency_key: newKey() } })), cardKeys)

export const useDecidePermission = () =>
  useInvalidating((a: { id: number } & S['PermissionDecision']) =>
    ok(api.POST('/api/permissions/{pid}/decide', { params: { path: { pid: a.id } }, body: { decision: a.decision, scope: a.scope, note: a.note } })), cardKeys)

export const useResolveTask = () =>
  useInvalidating((a: { id: number; outcome: 'done' | 'declined'; note?: string }) =>
    ok(api.POST('/api/tasks/{tid}/resolve', { params: { path: { tid: a.id } }, body: { outcome: a.outcome, note: a.note || null, idempotency_key: newKey() } })), cardKeys)

export const useDecideSuggestion = () =>
  useInvalidating((a: { id: number; decision: 'accept' | 'dismiss' }) =>
    ok(api.POST('/api/suggestions/{sid}/decide', { params: { path: { sid: a.id } }, body: { decision: a.decision } })), cardKeys)

export const useAcceptAll = () => useInvalidating(() => ok(api.POST('/api/suggestions/accept-all')), cardKeys)

export const useDecidePromote = () =>
  useInvalidating((a: { id: number; decision: 'approve' | 'deny'; note?: string }) =>
    ok(api.POST('/api/promote-requests/{pid}/decide', { params: { path: { pid: a.id } }, body: { decision: a.decision, note: a.note || null } })), cardKeys)

export const useDecideSecurity = () =>
  useInvalidating((a: { id: number; decision: 'confirm' | 'revoke' }) =>
    ok(api.POST('/api/security-alerts/{aid}/decide', { params: { path: { aid: a.id } }, body: { decision: a.decision } })), cardKeys)

export const useStopRun = () =>
  useInvalidating((id: number) => ok(api.POST('/api/runs/{run_id}/stop', { params: { path: { run_id: id } } })), [['status'], ['runs'], ['run']])

export const useLogin = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: S['LoginRequest']) => ok(api.POST('/api/auth/login', { body: b })),
    onSuccess: (r) => r.status === 'ok' && qc.invalidateQueries(),
  })
}
export const useTotp = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (b: S['TotpRequest']) => ok(api.POST('/api/auth/totp', { body: b })), onSuccess: () => qc.invalidateQueries() })
}

export const useRecent = (sender?: S['Message']['sender']) =>
  useQuery({ queryKey: ['threads', 'recent', sender ?? 'all'], queryFn: () => ok(api.GET('/api/messages/recent', { params: { query: { sender, limit: 5 } } })) })

export const useCreateThread = () =>
  useInvalidating((b: { title: string; project?: string | null; first_message?: string; upload_ids?: number[] }) =>
    ok(api.POST('/api/threads', { body: { ...b, idempotency_key: newKey() } })), [['threads'], ['projects']])

// ── M1: runs, account, system, setup ───────────────────────────────────────
export const useStartRun = () =>
  useInvalidating((b: S['RunStart']) => ok(api.POST('/api/runs', { body: b })), [['status'], ['runs']])

export const useSystem = () => useQuery({ queryKey: ['system'], queryFn: () => ok(api.GET('/api/system')), refetchInterval: 30_000 })
export const useSelftest = () => useMutation({ mutationFn: () => ok(api.POST('/api/system/selftest')) })

export const useSessions = () => useQuery({ queryKey: ['sessions'], queryFn: () => ok(api.GET('/api/account/sessions')) })
export const useRevokeSession = () =>
  useInvalidating((id: string) => ok(api.DELETE('/api/account/sessions/{session_id}', { params: { path: { session_id: id } } })), [['sessions'], ['inbox']])
export const useChangePassword = () =>
  useInvalidating((b: S['PasswordChange']) => ok(api.POST('/api/account/password', { body: b })), [['sessions']])
export const useTotpSetup = () => useMutation({ mutationFn: () => ok(api.POST('/api/account/totp/setup')) })
export const useTotpEnable = () => useInvalidating((b: S['TotpEnable']) => ok(api.POST('/api/account/totp/enable', { body: b })), [['me']])
export const useTotpDisable = () => useInvalidating((b: S['TotpDisable']) => ok(api.POST('/api/account/totp/disable', { body: b })), [['me']])

export const useSetupOwner = () => useMutation({ mutationFn: (b: S['SetupOwnerRequest']) => ok(api.POST('/api/setup/owner', { body: b })) })
export const useLogout = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: () => ok(api.POST('/api/auth/logout')), onSuccess: () => qc.invalidateQueries() })
}

/** The `detail` (or error code) from a failed request, for showing inline. */
export function problem(e: unknown): string | null {
  if (!e) return null
  const body = (e as { body?: { detail?: unknown; error?: unknown } }).body
  if (body && typeof body.detail === 'string' && body.detail) return body.detail
  if (body && typeof body.error === 'string') return body.error.replace(/_/g, ' ')
  return e instanceof Error ? e.message : 'Something went wrong.'
}

// ── M2: projects, threads, uploads, secrets ────────────────────────────────
export const useCreateProject = () =>
  useInvalidating((b: { name: string; summary?: string }) => ok(api.POST('/api/projects', { body: { name: b.name, summary: b.summary ?? '', idempotency_key: newKey() } })), [['projects']])

export const usePatchThread = (id: number) =>
  useInvalidating((b: S['ThreadPatch']) => ok(api.PATCH('/api/threads/{thread_id}', { params: { path: { thread_id: id } }, body: b })), [['thread'], ['threads'], ['projects']])

/** Upload a file into the project's (or General's) _inbox/ folder; returns the upload. */
export async function uploadFile(file: File, project: string | null): Promise<S['Upload']> {
  const form = new FormData()
  form.append('file', file)
  if (project) form.append('project', project)
  form.append('idempotency_key', newKey())
  const res = await globalThis.fetch('/api/uploads', { method: 'POST', body: form, credentials: 'include' })
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, body)
  return body as S['Upload']
}

export const useSecrets = () => useQuery({ queryKey: ['secrets'], queryFn: () => ok(api.GET('/api/secrets')) })
export const useSetSecret = () =>
  useInvalidating((a: { name: string } & S['SecretSet']) => ok(api.PUT('/api/secrets/{name}', { params: { path: { name: a.name } }, body: { value: a.value, description: a.description } })), [['secrets']])
export const useDeleteSecret = () =>
  useInvalidating((name: string) => ok(api.DELETE('/api/secrets/{name}', { params: { path: { name } } })), [['secrets']])

