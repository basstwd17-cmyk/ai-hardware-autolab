import { describe, expect, it } from 'vitest'
import type { HandoffDetail } from '../api/client'
import { handoffFacts, handoffSummary, handoffTitle, itemText } from './handoff'

const packet: HandoffDetail = {
  handoff_id: 'h1', job_id: 'job_1', iteration: 1, kind: 'SourcePacket', agent: 'researcher',
  step_id: '', at: '2026-08-31T00:00:00Z', sha256: 'a'.repeat(64), failure_signature: null,
  event_id: 4, payload: { summary: '  조사 결과를  정리했습니다. ', sources: [{ title: 'PRU guide' }], claims: [{ statement: 'DMA is suitable.' }] },
}

describe('handoff reading helpers', () => {
  it('uses a human title and only facts actually carried by the envelope', () => {
    expect(handoffTitle(packet.kind)).toBe('수집 자료')
    expect(handoffSummary(packet)).toBe('조사 결과를 정리했습니다.')
    expect(handoffFacts(packet)).toEqual([['자료', 1], ['근거', 1]])
  })

  it('picks an existing item label instead of inventing a paraphrase', () => {
    expect(itemText({ question: '핀 번호를 확인할 것' })).toBe('핀 번호를 확인할 것')
    expect(itemText({ unknown: 'not a displayed claim' })).toBe('')
  })
})
