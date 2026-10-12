/** Render only a control-plane question, never infer one from runtime chatter. */
export function clarificationQuestion(waiting: { status?: unknown; detail?: unknown } | null | undefined): string | null {
  if (waiting?.status !== 'waiting_person' || typeof waiting.detail !== 'string') return null
  return questionFromReason(waiting.detail)
}

export function questionFromReason(reason: string): string | null {
  return reason.startsWith('user_input:') ? reason.slice('user_input:'.length).trim() || null : null
}
