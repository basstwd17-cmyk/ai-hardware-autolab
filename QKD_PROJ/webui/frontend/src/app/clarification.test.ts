import { describe, expect, it } from 'vitest'
import { clarificationQuestion } from './clarification'

describe('operator clarification', () => {
  it('shows the full current control-plane question', () => {
    expect(clarificationQuestion({ status: 'waiting_person', detail: 'user_input: 프로브 배율은 얼마인가요?' })).toBe('프로브 배율은 얼마인가요?')
  })
  it('does not turn errors, approvals or a stale question into an answer request', () => {
    for (const waiting of [undefined, {status: 'waiting_person', detail: 'runtime_failure: timeout'},
      {status: 'waiting_approval', detail: 'user_input: wrong state'},
      {status: 'runnable', detail: 'user_input: old question'},
      {status: 'waiting_person', detail: 'user_input:   '}]) {
      expect(clarificationQuestion(waiting)).toBeNull()
    }
  })
})
