import type { S } from '../api/client'

// Model choices for roles, chats and levels. Claude providers use Claude Code's names
// (haiku, sonnet, opus) with an effort; opencode providers name a model from the list the
// owner enabled (opencode-go/glm-5.3), with an effort only where the model has variants.
// A tier is "model/effort" or just "model"; this mirrors budgets.split_tier on the server.

export const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const CLAUDE = ['haiku', 'sonnet', 'opus']

export function splitTier(tier: string): [string, string | null] {
  const i = tier.lastIndexOf('/')
  if (i > 0 && EFFORTS.includes(tier.slice(i + 1))) return [tier.slice(0, i), tier.slice(i + 1)]
  return [tier || 'sonnet', null]
}

export const joinTier = (model: string, effort: string | null) => (effort ? `${model}/${effort}` : model)

export interface ModelChoice { id: string; label: string; efforts: string[]; price?: string }

const money = (x?: number | null) => (x == null ? '?' : x === 0 ? 'free' : `$${x}`)

export function modelChoices(p?: S['ProviderInfo']): ModelChoice[] {
  if (!p?.models) return CLAUDE.map((m) => ({ id: m, label: m[0].toUpperCase() + m.slice(1), efforts: ['low', 'medium', 'high'] }))
  return p.models.map((m) => ({
    id: m.id, label: m.name, efforts: m.variants ?? [],
    price: m.input === 0 && m.output === 0 ? 'free' : `${money(m.input)} / ${money(m.output)} per M`,
  }))
}

/** A readable tier: "Sonnet · medium", "GLM-5.3 · high". */
export function tierLabel(tier: string, p?: S['ProviderInfo']): string {
  const [model, effort] = splitTier(tier)
  const c = modelChoices(p).find((m) => m.id === model)
  const name = c?.label ?? model.replace(/^[^/]+\//, '')
  return effort ? `${name} · ${effort}` : name
}
