import { describe, expect, it } from 'vitest'
import { promotionFrame } from './promotion'

describe('contract promotion frames', () => {
  it('keeps preview, request and apply as separate commands', () => {
    expect(promotionFrame('preview', 'job_1', '', 'p')).toEqual({
      type: 'contract.promotion.preview', commandId: 'p', jobId: 'job_1',
    })
    expect(promotionFrame('request', 'job_1', ' quantum1 ', 'r')).toEqual({
      type: 'contract.promotion.request', commandId: 'r', jobId: 'job_1', requestedBy: 'quantum1',
    })
    expect(promotionFrame('apply', 'approval_1', '', 'a')).toEqual({
      type: 'contract.promotion.apply', commandId: 'a', approvalId: 'approval_1',
    })
  })

  it('will not create an anonymous request', () => {
    expect(() => promotionFrame('request', 'job_1', ' ', 'r')).toThrow(/who/)
  })
})
