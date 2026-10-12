import type { LedgerEvent } from '../api/client'

export interface AgentTokenUsage {
  agent: string
  threads: number
  total: number
  input: number
  cachedInput: number
  output: number
  reasoningOutput: number
}

interface ThreadUsage extends AgentTokenUsage {
  eventId: number
}

/**
 * App Server reports cumulative token totals repeatedly while a thread runs.
 * Adding every notification would count the same prompt many times, so retain
 * only the newest cumulative value for each thread and then sum threads by
 * agent. Values are observations from the runtime, never cost estimates.
 */
export function tokenUsageByAgent(events: LedgerEvent[]): AgentTokenUsage[] {
  const threads = new Map<string, ThreadUsage>()

  for (const event of events) {
    if (event.kind !== 'runtime.thread/tokenUsage/updated') continue
    const params = (event.payload as { params?: unknown }).params as Record<string, unknown> | undefined
    const threadId = stringOf(params?.threadId)
    const tokenUsage = params?.tokenUsage as Record<string, unknown> | undefined
    const total = tokenUsage?.total as Record<string, unknown> | undefined
    if (!threadId || !total) continue
    const previous = threads.get(threadId)
    if (previous && previous.eventId >= event.event_id) continue
    threads.set(threadId, {
      eventId: event.event_id,
      agent: event.agent?.trim() || 'unknown-agent',
      threads: 1,
      total: countOf(total.totalTokens),
      input: countOf(total.inputTokens),
      cachedInput: countOf(total.cachedInputTokens),
      output: countOf(total.outputTokens),
      reasoningOutput: countOf(total.reasoningOutputTokens),
    })
  }

  const agents = new Map<string, AgentTokenUsage>()
  for (const thread of threads.values()) {
    const row = agents.get(thread.agent) ?? {
      agent: thread.agent,
      threads: 0,
      total: 0,
      input: 0,
      cachedInput: 0,
      output: 0,
      reasoningOutput: 0,
    }
    row.threads += 1
    row.total += thread.total
    row.input += thread.input
    row.cachedInput += thread.cachedInput
    row.output += thread.output
    row.reasoningOutput += thread.reasoningOutput
    agents.set(thread.agent, row)
  }

  return [...agents.values()].sort((left, right) => right.total - left.total || left.agent.localeCompare(right.agent))
}

function stringOf(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function countOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0
}
