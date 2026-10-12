import { describe, expect, it } from 'vitest'
import type { LedgerEvent } from '../api/client'
import { tokenUsageByAgent } from './tokenUsage'

function usage(
  eventId: number,
  agent: string,
  threadId: string,
  totalTokens: number,
  inputTokens = totalTokens - 100,
  outputTokens = 100,
): LedgerEvent {
  return {
    event_id: eventId,
    job_id: 'job_1',
    seq: eventId,
    at: '2026-09-02T00:00:00Z',
    kind: 'runtime.thread/tokenUsage/updated',
    agent,
    payload: {
      method: 'thread/tokenUsage/updated',
      params: {
        threadId,
        tokenUsage: {
          total: {
            totalTokens,
            inputTokens,
            cachedInputTokens: 40,
            outputTokens,
            reasoningOutputTokens: 20,
          },
        },
      },
    },
  }
}

describe('per-agent token usage', () => {
  it('uses only the newest cumulative value from one thread', () => {
    const [row] = tokenUsageByAgent([
      usage(1, 'researcher', 'thread-a', 1000),
      usage(2, 'researcher', 'thread-a', 2400),
    ])
    expect(row.total).toBe(2400)
    expect(row.threads).toBe(1)
  })

  it('sums separate invocations of the same agent and keeps agents separate', () => {
    const rows = tokenUsageByAgent([
      usage(1, 'researcher', 'thread-a', 1000),
      usage(2, 'researcher', 'thread-b', 2000),
      usage(3, 'verifier', 'thread-c', 500),
    ])
    expect(rows.map((row) => [row.agent, row.total, row.threads])).toEqual([
      ['researcher', 3000, 2],
      ['verifier', 500, 1],
    ])
  })

  it('ignores unrelated and incomplete runtime events', () => {
    const incomplete = usage(1, 'researcher', '', 1000)
    const unrelated = { ...usage(2, 'researcher', 'thread-a', 2000), kind: 'runtime.item/completed' }
    expect(tokenUsageByAgent([incomplete, unrelated])).toEqual([])
  })
})
