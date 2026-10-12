import { describe, expect, it } from 'vitest'
import { approvalDecisionFrame } from './approval'

describe('human approval wire frame', () => {
  it('uses the protocol decision words and records the signer', () => {
    expect(approvalDecisionFrame('approval_1', 'accepted', ' quantum1 ', ' reviewed ', 'c1')).toEqual({
      type: 'approval.decide',
      commandId: 'c1',
      approvalId: 'approval_1',
      decision: 'accepted',
      signedBy: 'quantum1',
      note: 'reviewed',
    })
  })

  it('refuses an anonymous decision', () => {
    expect(() => approvalDecisionFrame('approval_1', 'declined', '  ', '', 'c1')).toThrow(/who/)
  })
})
