/** Preferences belong to the browser; submitted copies belong to the job. */
export const MODEL_ROLES = ['default', 'lead-agent', 'builder', 'verifier'] as const
export type ModelRole = typeof MODEL_ROLES[number]
export type ModelChoice = { model: string; effort?: string }
export type ModelSelection = Partial<Record<ModelRole, ModelChoice>>
export interface ModelOption {
  model: string
  displayName: string
  defaultEffort: string | null
  efforts: string[]
  isDefault: boolean
}
export const MODEL_STORAGE_KEY = 'qkd.model-selection.v1'
export const MODEL_CHANGE_EVENT = 'qkd-model-selection-changed'

export function readModelSelection(raw: string | null): ModelSelection {
  try {
    const value = JSON.parse(raw ?? '{}') as Record<string, unknown>
    if (!value || Array.isArray(value) || typeof value !== 'object') return {}
    const result: ModelSelection = {}
    for (const role of MODEL_ROLES) {
      const entry = value[role] as ModelChoice | undefined
      if (entry && typeof entry.model === 'string' && entry.model.trim()) {
        result[role] = { model: entry.model, ...(typeof entry.effort === 'string' ? { effort: entry.effort } : {}) }
      }
    }
    return result
  } catch { return {} }
}

export function setModelChoice(selection: ModelSelection, role: ModelRole, model: string): ModelSelection {
  const next = { ...selection }
  if (model) next[role] = { model } // A different model must not inherit an unsupported effort.
  else delete next[role]
  return next
}

export function withModelSelection(frame: Record<string, unknown>, selection: ModelSelection): Record<string, unknown> {
  if (!Object.keys(selection).length) return frame
  // Snapshot: subsequent Settings edits must not mutate a submitted frame.
  return { ...frame, modelSelection: readModelSelection(JSON.stringify(selection)) }
}
