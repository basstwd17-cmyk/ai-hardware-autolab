/** Exact wire frames for a human decision. No truthy-string coercion. */

export type HumanDecision = 'accepted' | 'declined'

export function approvalDecisionFrame(
  approvalId: string,
  decision: HumanDecision,
  signedBy: string,
  note = '',
  commandId: string = crypto.randomUUID(),
): Record<string, unknown> {
  const signer = signedBy.trim()
  if (!signer) throw new Error('Enter who is signing this decision.')
  if (!approvalId.trim()) throw new Error('The approval id is missing.')
  return {
    type: 'approval.decide',
    commandId,
    approvalId,
    decision,
    signedBy: signer,
    note: note.trim(),
  }
}
