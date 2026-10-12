export type PromotionAction = 'preview' | 'request' | 'apply'

export function promotionFrame(
  action: PromotionAction,
  value: string,
  requestedBy = '',
  commandId: string = crypto.randomUUID(),
): Record<string, unknown> {
  if (!value.trim()) throw new Error(action === 'apply' ? 'The approval id is missing.' : 'The job id is missing.')
  if (action === 'request' && !requestedBy.trim()) throw new Error('Enter who is requesting approval.')
  if (action === 'apply') {
    return { type: 'contract.promotion.apply', commandId, approvalId: value }
  }
  return {
    type: `contract.promotion.${action}`,
    commandId,
    jobId: value,
    ...(action === 'request' ? { requestedBy: requestedBy.trim() } : {}),
  }
}
