import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, CLIENT_HEADERS, newKey, ok, type S } from './client'
import { clearOffline, enqueue, flushOutbox, isNetworkError, type OutboxItem } from './offline'

/** Send now, or queue on this device when offline (only messages, new threads, answers and
 * finished tasks: never approvals, secrets, settings or budgets). Returns null when queued. */
async function orQueue<T>(item: OutboxItem, send: () => Promise<T>): Promise<T | null> {
  if (!navigator.onLine) {
    await enqueue(item)
    return null
  }
  try {
    return await send()
  } catch (e) {
    if (isNetworkError(e)) {
      await enqueue(item)
      return null
    }
    throw e
  }
}
const now = () => new Date().toISOString()

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
  useQuery({ queryKey: ['thread', id, 'delivery'], queryFn: () => ok(api.GET('/api/threads/{thread_id}/delivery', { params: { path: { thread_id: id } } })), enabled: id > 0 })

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
    const b = { body, upload_ids, idempotency_key: newKey() }
    return orQueue({ kind: 'message', thread: threadId, body: b, at: now() },
      () => ok(api.POST('/api/threads/{thread_id}/messages', { params: { path: { thread_id: threadId } }, body: b })))
  }, [['thread'], ['threads']])

export const useReplyNow = (threadId: number) =>
  useInvalidating(() => ok(api.POST('/api/threads/{thread_id}/reply-now', { params: { path: { thread_id: threadId } } })), [['status'], ['runs'], ['thread']])

export const useAnswer = () =>
  useInvalidating((a: { id: number; value: unknown; note?: string }) => {
    const b = { value: a.value, note: a.note || null, idempotency_key: newKey() }
    return orQueue({ kind: 'answer', question: a.id, body: b, at: now() }, () => ok(api.POST('/api/questions/{qid}/answer', { params: { path: { qid: a.id } }, body: b })))
  }, cardKeys)

export const useDecidePermission = () =>
  useInvalidating((a: { id: number } & S['PermissionDecision']) =>
    ok(api.POST('/api/permissions/{pid}/decide', { params: { path: { pid: a.id } }, body: { decision: a.decision, scope: a.scope, note: a.note } })), cardKeys)

export const useResolveTask = () =>
  useInvalidating((a: { id: number; outcome: 'done' | 'declined'; note?: string }) => {
    const b = { outcome: a.outcome, note: a.note || null, idempotency_key: newKey() }
    return orQueue({ kind: 'task', task: a.id, body: b, at: now() }, () => ok(api.POST('/api/tasks/{tid}/resolve', { params: { path: { tid: a.id } }, body: b })))
  }, cardKeys)

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
    onSuccess: (r) => { if (r.status === 'ok') { void qc.invalidateQueries(); void flushOutbox(qc) } },
  })
}
export const useTotp = () => {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (b: S['TotpRequest']) => ok(api.POST('/api/auth/totp', { body: b })), onSuccess: () => { void qc.invalidateQueries(); void flushOutbox(qc) } })
}

export const useRecent = (sender?: S['Message']['sender']) =>
  useQuery({ queryKey: ['threads', 'recent', sender ?? 'all'], queryFn: () => ok(api.GET('/api/messages/recent', { params: { query: { sender, limit: 5 } } })) })

export const useCreateThread = () =>
  useInvalidating((b: { title: string; project?: string | null; first_message?: string; upload_ids?: number[] }) => {
    const body = { ...b, idempotency_key: newKey() }
    return orQueue({ kind: 'thread', body, at: now() }, () => ok(api.POST('/api/threads', { body })))
  }, [['threads'], ['projects']])

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
  return useMutation({ mutationFn: () => ok(api.POST('/api/auth/logout')), onSuccess: async () => { await clearOffline(); qc.clear(); qc.invalidateQueries() } })
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
  const res = await globalThis.fetch('/api/uploads', { method: 'POST', body: form, credentials: 'include', headers: CLIENT_HEADERS })
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, body)
  return body as S['Upload']
}

export async function uploadChatFile(file: File): Promise<S['ChatFile']> {
  const form = new FormData()
  form.append('file', file)
  const res = await globalThis.fetch('/api/chat-files', { method: 'POST', body: form, credentials: 'include', headers: CLIENT_HEADERS })
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new ApiError(res.status, body)
  return body as S['ChatFile']
}

export const useSecrets = () => useQuery({ queryKey: ['secrets'], queryFn: () => ok(api.GET('/api/secrets')) })
export const useSetSecret = () =>
  useInvalidating((a: { name: string } & S['SecretSet']) => ok(api.PUT('/api/secrets/{name}', { params: { path: { name: a.name } }, body: { value: a.value, description: a.description, scope: a.scope } })), [['secrets']])
export const useDeleteSecret = () =>
  useInvalidating((name: string) => ok(api.DELETE('/api/secrets/{name}', { params: { path: { name } } })), [['secrets']])

// ── M6: network (egress rules and the request log; online only) ────────────
export type NetFilter = 'all' | 'blocked' | 'get' | 'post' | 'hour'
export const useNetwork = (filter: NetFilter = 'all') =>
  useQuery({ queryKey: ['network', filter], refetchInterval: 15_000, queryFn: () => ok(api.GET('/api/network', { params: { query: { filter } } })) })
export const useAddEgressRule = () =>
  useInvalidating((b: Omit<S['EgressRuleCreate'], 'idempotency_key'>) => ok(api.POST('/api/network/rules', { body: { ...b, idempotency_key: newKey() } })), [['network']])
export const useDeleteEgressRule = () =>
  useInvalidating((id: number) => ok(api.DELETE('/api/network/rules/{rule_id}', { params: { path: { rule_id: id } } })), [['network']])

// ── M6: integrations and connected apps (online only) ─────────────────────
const intKeys = [['integrations']]
export const useIntegrations = () => useQuery({ queryKey: ['integrations'], queryFn: () => ok(api.GET('/api/integrations')) })
export const useIntegrationPresets = () => useQuery({ queryKey: ['integrations', 'presets'], staleTime: 300_000, queryFn: () => ok(api.GET('/api/integrations/presets')) })
export const useAddIntegration = () =>
  useInvalidating((b: Omit<S['IntegrationCreate'], 'idempotency_key'>) => ok(api.POST('/api/integrations', { body: { ...b, idempotency_key: newKey() } })), [...intKeys, ['secrets']])
export const useUpdateIntegration = () =>
  useInvalidating((a: { name: string } & S['IntegrationPatch']) => {
    const { name, ...body } = a
    return ok(api.PATCH('/api/integrations/{name}', { params: { path: { name } }, body }))
  }, intKeys)
export const useDeleteIntegration = () =>
  useInvalidating((name: string) => ok(api.DELETE('/api/integrations/{name}', { params: { path: { name } } })), intKeys)
export const useSyncIntegration = () =>
  useInvalidating((name: string) => ok(api.POST('/api/integrations/{name}/sync', { params: { path: { name } } })), intKeys)
export const useSetToolPolicy = () =>
  useInvalidating((a: { name: string; tool: string; policy: S['IntegrationTool']['policy'] }) =>
    ok(api.PUT('/api/integrations/{name}/tools/{tool}', { params: { path: { name: a.name, tool: a.tool } }, body: { policy: a.policy } })), intKeys)
export const useConnectedApps = () => useQuery({ queryKey: ['connected-apps'], queryFn: () => ok(api.GET('/api/connected-apps')) })
export const useRevokeApp = () =>
  useInvalidating((id: number) => ok(api.DELETE('/api/connected-apps/{app_id}', { params: { path: { app_id: id } } })), [['connected-apps']])

// ── M3: files, shelf, web page ─────────────────────────────────────────────
export const useFileHistory = (volume: S['Listing']['volume'], path: string, enabled = true) =>
  useQuery({ queryKey: ['files', 'history', volume, path], enabled, queryFn: () => ok(api.GET('/api/files/history', { params: { query: { volume, path } } })) })
export const useFileDiff = (volume: S['Listing']['volume'], path: string, rev: string | null) =>
  useQuery({ queryKey: ['files', 'diff', volume, path, rev], enabled: !!rev, queryFn: () => ok(api.GET('/api/files/diff', { params: { query: { volume, path, rev: rev! } } })) })
export const useShelfVersions = (id: number | null) =>
  useQuery({ queryKey: ['shelf', 'versions', id], enabled: id != null, queryFn: () => ok(api.GET('/api/shelf/{item_id}/versions', { params: { path: { item_id: id! } } })) })
export const useAddToShelf = () =>
  useInvalidating((a: { project: string } & Omit<S['ShelfCreate'], 'idempotency_key'>) =>
    ok(api.POST('/api/projects/{slug}/shelf', { params: { path: { slug: a.project } }, body: { volume: a.volume, path: a.path, label: a.label, mode: a.mode, keep_versions: a.keep_versions, idempotency_key: newKey() } })),
  [['shelf'], ['projects']])
export const useRemoveFromShelf = () =>
  useInvalidating((id: number) => ok(api.DELETE('/api/shelf/{item_id}', { params: { path: { item_id: id } } })), [['shelf'], ['projects']])
export const useWebPage = () => useQuery({ queryKey: ['web'], queryFn: () => ok(api.GET('/api/web')), refetchInterval: 60_000 })

// ── M4: budgets, providers, roles, the reviewer, the calendar, routines ────
// All of these changes are online only (never queued): budgets and settings.
const budgetKeys = [['budget'], ['status'], ['calendar'], ['thread']]
const P = 'claude-subscription'

export const useBudget = () => useQuery({ queryKey: ['budget'], queryFn: () => ok(api.GET('/api/budget')) })
// Each takes the provider whose budget changes (the overview lists the worker's provider first).
export const usePatchBudget = () =>
  useInvalidating(({ provider = P, ...b }: S['BudgetPatch'] & { provider?: string }) => ok(api.PATCH('/api/budget/{provider}', { params: { path: { provider } }, body: b })), budgetKeys)
export const useBoost = () =>
  useInvalidating(({ provider = P, ...b }: S['BoostRequest'] & { provider?: string }) => ok(api.POST('/api/budget/{provider}/boost', { params: { path: { provider } }, body: b })), budgetKeys)
export const useClearBoost = () =>
  useInvalidating((provider: string = P) => ok(api.DELETE('/api/budget/{provider}/boost', { params: { path: { provider } } })), budgetKeys)
export const usePatchReserve = () => useInvalidating((b: S['ReserveSettings']) => ok(api.PATCH('/api/budget', { body: b })), budgetKeys)

export const useProviderCatalog = (id: string, enabled = true) =>
  useQuery({ queryKey: ['budget', 'catalog', id], queryFn: () => ok(api.GET('/api/providers/{provider}/catalog', { params: { path: { provider: id } } })), enabled, staleTime: 3600_000 })
export const useProviders = () => useQuery({ queryKey: ['budget', 'providers'], queryFn: () => ok(api.GET('/api/providers')) })
export const useAddApiProvider = () => useInvalidating((b: S['ApiProviderCreate']) => ok(api.POST('/api/providers', { body: b })), budgetKeys)
export const usePatchProvider = () =>
  useInvalidating((a: { id: string } & S['ProviderPatch']) => ok(api.PATCH('/api/providers/{provider}', { params: { path: { provider: a.id } }, body: { enabled: a.enabled, label: a.label, models: a.models } })), budgetKeys)
export const useDeleteProvider = () => useInvalidating((id: string) => ok(api.DELETE('/api/providers/{provider}', { params: { path: { provider: id } } })), budgetKeys)

export const useRoles = () => useQuery({ queryKey: ['budget', 'roles'], queryFn: () => ok(api.GET('/api/roles')) })
export const useSetRoles = () => useInvalidating((b: S['Roles']) => ok(api.PUT('/api/roles', { body: b })), [['budget']])
export const useReviewer = () => useQuery({ queryKey: ['budget', 'reviewer'], queryFn: () => ok(api.GET('/api/reviewer')) })
export const useSetReviewer = () => useInvalidating((b: S['ReviewerConfig']) => ok(api.PUT('/api/reviewer', { body: b })), [['budget']])
export const useInstructions = () => useQuery({ queryKey: ['budget', 'instructions'], queryFn: () => ok(api.GET('/api/instructions')) })
export const useSetInstructions = () => useInvalidating((text: string) => ok(api.PUT('/api/instructions', { body: { text } })), [['budget']])

export const useCalendar = (start?: string, end?: string) =>
  useQuery({ queryKey: ['calendar', start ?? '', end ?? ''], queryFn: () => ok(api.GET('/api/calendar', { params: { query: { start, end } } })) })
export const useCreateWakeup = () => useInvalidating((b: S['WakeupCreate']) => ok(api.POST('/api/wakeups', { body: b })), budgetKeys)
export const usePatchWakeup = () =>
  useInvalidating((a: { id: number } & S['WakeupPatch']) => {
    const { id, ...body } = a
    return ok(api.PATCH('/api/wakeups/{wakeup_id}', { params: { path: { wakeup_id: id } }, body }))
  }, budgetKeys)
export const useCancelWakeup = () => useInvalidating((id: number) => ok(api.DELETE('/api/wakeups/{wakeup_id}', { params: { path: { wakeup_id: id } } })), budgetKeys)

export const useRoutines = () => useQuery({ queryKey: ['calendar', 'routines'], queryFn: () => ok(api.GET('/api/routines')) })
export const useCreateRoutine = () => useInvalidating((b: S['RoutineCreate']) => ok(api.POST('/api/routines', { body: b })), budgetKeys)
export const usePatchRoutine = () =>
  useInvalidating((a: { id: number } & S['RoutinePatch']) => {
    const { id, ...body } = a
    return ok(api.PATCH('/api/routines/{routine_id}', { params: { path: { routine_id: id } }, body }))
  }, budgetKeys)
export const useDeleteRoutine = () => useInvalidating((id: number) => ok(api.DELETE('/api/routines/{routine_id}', { params: { path: { routine_id: id } } })), budgetKeys)

// ── M5: updates, notifications, logging the agent in ───────────────────────
export const useUpdateInfo = () => useQuery({ queryKey: ['system', 'update'], queryFn: () => ok(api.GET('/api/system/update')), refetchInterval: 30 * 60_000 })
export const useCancelUpdate = () => useInvalidating(() => ok(api.POST('/api/system/update/cancel')), [['system'], ['status']])
export const useCheckUpdate = () => useInvalidating(() => ok(api.POST('/api/system/update/check')), [['system']])
export const useNotificationSettings = () => useQuery({ queryKey: ['notifications'], queryFn: () => ok(api.GET('/api/notifications/settings')) })
export const useSetNotificationSettings = () =>
  useInvalidating((b: S['NotificationSettings']) => ok(api.PUT('/api/notifications/settings', { body: b })), [['notifications']])
export const usePushTest = () => useMutation({ mutationFn: () => ok(api.POST('/api/push/test')) })
export const useRemovePushSubscription = () =>
  useInvalidating((id: number) => ok(api.DELETE('/api/push/subscriptions/{sub_id}', { params: { path: { sub_id: id } } })), [['notifications']])

/** Ask the browser for notification permission and register this device for web push. */
export async function subscribePush(label: string): Promise<void> {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('This browser has no web push (on iPhone, add the app to the Home Screen first).')
  if (!window.isSecureContext) throw new Error('Notifications need HTTPS: open this app at its https:// address (see the install docs).')
  const perm = await Notification.requestPermission()
  if (perm !== 'granted') throw new Error('Notifications are blocked for this site in the browser settings.')
  const reg = (await navigator.serviceWorker.getRegistration()) ?? (await navigator.serviceWorker.register('/sw.js'))
  await navigator.serviceWorker.ready
  const { public_key } = await ok(api.GET('/api/push/key'))
  const pad = '='.repeat((4 - (public_key.length % 4)) % 4)
  const raw = Uint8Array.from(atob((public_key + pad).replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0))
  const sub = (await reg.pushManager.getSubscription()) ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: raw }))
  const j = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } }
  await ok(api.POST('/api/push/subscriptions', { body: { endpoint: j.endpoint, keys: j.keys, label } }))
}

export const useClaudeLoginStart = () => useMutation({ mutationFn: () => ok(api.POST('/api/providers/claude-subscription/login')) })
export const useClaudeLoginCode = () =>
  useInvalidating((code: string) => ok(api.POST('/api/providers/claude-subscription/login/code', { body: { code } })), [['system'], ['status'], ['budget']])

// ── 1:1 chats (M6) ─────────────────────────────────────────────────────────
// Sending a chat message is online only: replies stream from the model right away.
export const useChats = (archived = false) => useQuery({ queryKey: ['chats', archived], queryFn: () => ok(api.GET('/api/chats', { params: { query: { archived } } })) })
export const useChat = (id: number) => useQuery({ queryKey: ['chat', String(id)], queryFn: () => ok(api.GET('/api/chats/{chat_id}', { params: { path: { chat_id: id } } })), enabled: id > 0 })
export const useChatMessages = (id: number) =>
  useQuery({ queryKey: ['chat', String(id), 'messages'], queryFn: () => ok(api.GET('/api/chats/{chat_id}/messages', { params: { path: { chat_id: id } } })), enabled: id > 0 })
const chatKeys = [['chats'], ['chat']]
export const useCreateChat = () => useInvalidating((b: S['ChatCreate']) => ok(api.POST('/api/chats', { body: { ...b, idempotency_key: b.idempotency_key ?? newKey() } })), chatKeys)
export const useSendChat = (id: number) =>
  useInvalidating((m: string | { body: string; upload_ids: number[] }) => {
    const { body, upload_ids = [] } = typeof m === 'string' ? { body: m } : m
    return ok(api.POST('/api/chats/{chat_id}/messages', { params: { path: { chat_id: id } }, body: { body, upload_ids, idempotency_key: newKey() } }))
  }, chatKeys)
export const usePatchChat = (id: number) => useInvalidating((b: S['ChatPatch']) => ok(api.PATCH('/api/chats/{chat_id}', { params: { path: { chat_id: id } }, body: b })), chatKeys)
export const useStopChat = (id: number) => useInvalidating(() => ok(api.POST('/api/chats/{chat_id}/stop', { params: { path: { chat_id: id } } })), chatKeys)
export const useDecideChatCard = (id: number) =>
  useInvalidating((a: { message_id: number } & S['ChatCardDecision']) =>
    ok(api.POST('/api/chats/{chat_id}/cards/{message_id}', { params: { path: { chat_id: id, message_id: a.message_id } }, body: { decision: a.decision, value: a.value } })),
  [...chatKeys, ['secrets'], ['budget'], ['calendar'], ['integrations']])
export const useChatHandoff = (id: number) =>
  useInvalidating((b: S['ChatHandoff']) => ok(api.POST('/api/chats/{chat_id}/handoff', { params: { path: { chat_id: id } }, body: b })), [...chatKeys, ['threads']])
export const useChatAsk = (id: number) =>
  useInvalidating((b: S['ChatAsk']) => ok(api.POST('/api/chats/{chat_id}/ask', { params: { path: { chat_id: id } }, body: b })), [...chatKeys, ['thread']])
export const useChatPull = (id: number) =>
  useInvalidating((thread_id: number) => ok(api.POST('/api/chats/{chat_id}/context', { params: { path: { chat_id: id } }, body: { thread_id } })), chatKeys)
