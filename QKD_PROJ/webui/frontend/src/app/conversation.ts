/**
 * A job's ledger rows, read as a conversation.
 *
 * The rows are already a transcript — they are written in order, each carries
 * the agent that produced it, and the runtime ones carry what the agent
 * actually said or ran. What this module does is *group* them, and the
 * grouping rule is the one thing worth arguing about.
 *
 * **A turn ends at a handoff.** `handoff.Plan`, `handoff.StepResult`,
 * `handoff.Verdict`, `handoff.Report` are the platform's own turn boundaries:
 * an agent works, then hands something to the next one. So a turn is "the
 * runtime rows produced by one agent, up to and including the handoff that
 * closed it". That is the ledger's structure, not a shape imposed on it.
 *
 * **Handoff contents are not here, and the UI says so.** The event stream
 * carries a handoff as `{iteration, agent, step_id, sha256}` — the digest of
 * the envelope, not the envelope. The Plan text, the Verdict conditions and
 * the Report prose live in the `handoffs` table, which no read command
 * exposes. So a handoff renders as what it is: a named, digested handover.
 * The one exception is the Verdict, which `/api/jobs/{id}` returns verbatim
 * inside `result` because the daemon reduces it there.
 *
 * **Nothing here decides a state.** `job.blocked`, `job.done` and the rest are
 * rendered as the markers they are. Whether the job *is* done is the control
 * plane's answer, shown elsewhere on the screen.
 */

import type { LedgerEvent } from '../api/client'

export type EntryKind =
  | 'brief'
  | 'message'
  | 'command'
  | 'files'
  | 'reasoning'
  | 'subagent'
  | 'collab'
  | 'handoff'
  | 'approval'
  | 'marker'
  | 'other'

export interface Entry {
  eventId: number
  at: string
  kind: EntryKind
  /** Short label for the bubble's header, e.g. `ran a command`. */
  title: string
  /** The body, already flattened to text where there is one. */
  body: string
  /** Extra facts worth showing as small print, in order. */
  facts: Array<[string, string]>
  /** True when the payload was clipped by the recorder at 8 KB. */
  truncated: boolean
}

export interface Turn {
  agent: string
  iteration: number | null
  entries: Entry[]
  /** The handoff that closed this turn, if it has been closed. */
  closedBy: { kind: string; sha256: string; stepId: string } | null
}

/** A readable projection of a turn.  It hides no ledger row: `entries` is the
 * expandable audit trail, while the other fields are the small default feed. */
export interface FeedStep {
  agent: string
  iteration: number | null
  entries: Entry[]
  closedBy: Turn['closedBy']
  title: string
  summary: string
  chips: string[]
  at: string
}

const HANDOFF = 'handoff.'
const RUNTIME = 'runtime.'

/** The agent a row belongs to; `human` for rows a person caused. */
function actorOf(event: LedgerEvent): string {
  if (event.agent && event.agent.trim()) return event.agent.trim()
  if (event.kind === 'job.created') return 'human'
  return 'platform'
}

export function toTurns(events: LedgerEvent[]): Turn[] {
  const turns: Turn[] = []
  let iteration: number | null = null

  for (const event of events) {
    if (event.kind === 'iteration.started') {
      const value = (event.payload as { iteration?: unknown }).iteration
      iteration = typeof value === 'number' ? value : iteration
    }

    const entry = toEntry(event)
    if (!entry) continue

    const agent = actorOf(event)
    let turn = turns[turns.length - 1]
    // A new turn when the actor changes, or when the last one was closed by a
    // handoff: an agent that hands off and is then asked again is doing a
    // second turn, not continuing the first.
    if (!turn || turn.agent !== agent || turn.closedBy) {
      turn = { agent, iteration, entries: [], closedBy: null }
      turns.push(turn)
    }
    turn.entries.push(entry)

    if (event.kind.startsWith(HANDOFF)) {
      const payload = event.payload as { sha256?: unknown; step_id?: unknown }
      turn.closedBy = {
        kind: event.kind.slice(HANDOFF.length),
        sha256: typeof payload.sha256 === 'string' ? payload.sha256 : '',
        stepId: typeof payload.step_id === 'string' ? payload.step_id : '',
      }
    }
  }
  return turns
}

/**
 * Collapse a turn into one feed card without replacing its evidence trail.
 * Runtime counters and command/file rows are useful as concise chips; their
 * raw content remains in the card's expandable details.
 */
export function toFeedSteps(turns: Turn[]): FeedStep[] {
  return turns.map((turn) => {
    const visible = turn.entries.filter((entry) => entry.kind !== 'marker')
    const narrative = visible.find((entry) => (
      (entry.kind === 'message' || entry.kind === 'reasoning' || entry.kind === 'brief') && entry.body
    ))
    const commands = visible.filter((entry) => entry.kind === 'command')
    const files = visible.filter((entry) => entry.kind === 'files')
    const approvals = visible.filter((entry) => entry.kind === 'approval')
    const chips = [
      commands.length ? `명령 ${commands.length}` : '',
      files.length ? `파일 ${files.length}` : '',
      approvals.length ? `승인 ${approvals.length}` : '',
      turn.closedBy ? handoffLabel(turn.closedBy.kind) : '',
    ].filter(Boolean)
    const title = turn.closedBy
      ? handoffLabel(turn.closedBy.kind)
      : narrative?.kind === 'brief'
        ? '시나리오 등록'
        : visible.length
          ? '진행 중'
          : '기록 대기'
    return {
      agent: turn.agent,
      iteration: turn.iteration,
      entries: turn.entries,
      closedBy: turn.closedBy,
      title,
      summary: narrative?.body ?? summaryFromActivities(visible),
      chips,
      at: turn.entries[0]?.at ?? '',
    }
  })
}

function toEntry(event: LedgerEvent): Entry | null {
  const base = {
    eventId: event.event_id,
    at: event.at,
    facts: [] as Array<[string, string]>,
    truncated: false,
  }

  if (event.kind === 'job.created') {
    const payload = event.payload as { objective?: unknown; gate?: unknown }
    return {
      ...base,
      kind: 'brief',
      title: 'opened the job',
      body: text(payload.objective),
      facts: [['contract', text(payload.gate)]],
    }
  }

  if (event.kind.startsWith(HANDOFF)) {
    const payload = event.payload as { sha256?: unknown; step_id?: unknown }
    const kind = event.kind.slice(HANDOFF.length)
    return {
      ...base,
      kind: 'handoff',
      title: `handed off a ${kind}`,
      // Said plainly rather than left as an empty bubble: the stream carries
      // the digest, and the contents are not something this page can read.
      body: '',
      facts: [
        ['sha256', text(payload.sha256)],
        ...(text(payload.step_id) ? ([['step', text(payload.step_id)]] as Array<[string, string]>) : []),
      ],
    }
  }

  if (event.kind.startsWith('approval.')) {
    const payload = event.payload as Record<string, unknown>
    return {
      ...base,
      kind: 'approval',
      title: event.kind.replace('approval.', 'approval '),
      body: '',
      facts: Object.entries(payload).map(([k, v]) => [k, text(v)] as [string, string]),
    }
  }

  if (event.kind.startsWith('campaign.')) {
    const payload = event.payload as Record<string, unknown>
    return {
      ...base,
      kind: 'approval',
      title: event.kind.replace('campaign.', 'campaign '),
      body: text(payload.reason),
      facts: Object.entries(payload)
        .filter(([key]) => key !== 'reason')
        .map(([key, value]) => [key, text(value)] as [string, string]),
    }
  }

  if (event.kind.startsWith(RUNTIME)) return runtimeEntry(event, base)

  if (event.kind.startsWith('job.') || event.kind === 'iteration.started') {
    const payload = event.payload as Record<string, unknown>
    return {
      ...base,
      kind: 'marker',
      title: event.kind,
      body: '',
      facts: Object.entries(payload).map(([k, v]) => [k, text(v)] as [string, string]),
    }
  }
  return null
}

type Base = Pick<Entry, 'eventId' | 'at' | 'facts' | 'truncated'>

function runtimeEntry(event: LedgerEvent, base: Base): Entry | null {
  const payload = event.payload as {
    method?: unknown
    params?: unknown
    item_type?: unknown
  }
  const method = text(payload.method)
  const itemType = text(payload.item_type)

  // The recorder clips a payload over 8 KB to `{truncated, bytes, head}`. That
  // is a real state of the record, so it is shown rather than papered over.
  const params = payload.params
  if (isTruncated(params)) {
    return {
      ...base,
      truncated: true,
      kind: 'other',
      title: itemType || method,
      body: text((params as { head?: unknown }).head),
      facts: [['clipped at', `${text((params as { bytes?: unknown }).bytes)} bytes`]],
    }
  }

  const item = (params as { item?: unknown } | null)?.item as Record<string, unknown> | undefined
  // Only the completion carries the finished content; the start of the same
  // item would render as a duplicate with less in it.
  const finished = method.endsWith('/completed')

  switch (itemType) {
    case 'agentMessage':
      if (!finished) return null
      return { ...base, kind: 'message', title: 'said', body: text(item?.text), facts: [] }

    case 'userMessage':
      if (!finished) return null
      return { ...base, kind: 'message', title: 'was told', body: text(item?.content), facts: [] }

    case 'reasoning':
      if (!finished) return null
      return {
        ...base,
        kind: 'reasoning',
        title: 'reasoned',
        body: summaryOf(item?.summary) || text(item?.content),
        facts: [],
      }

    case 'commandExecution': {
      if (!finished) return null
      const exit = item?.exitCode
      return {
        ...base,
        kind: 'command',
        title: 'ran a command',
        body: text(item?.command),
        facts: [
          ['exit', exit === undefined || exit === null ? '—' : String(exit)],
          ['status', text(item?.status)],
          ['cwd', text(item?.cwd)],
          ['output', text(item?.aggregatedOutput)],
        ].filter(([, v]) => v && v !== '—') as Array<[string, string]>,
      }
    }

    case 'fileChange': {
      if (!finished) return null
      const changes = Array.isArray(item?.changes) ? (item?.changes as unknown[]) : []
      return {
        ...base,
        kind: 'files',
        title: `changed ${changes.length} file${changes.length === 1 ? '' : 's'}`,
        body: '',
        facts: changes.slice(0, 12).map((change, index) => {
          const row = change as Record<string, unknown>
          return [text(row.path) || `change ${index + 1}`, text(row.kind ?? row.type)] as [
            string,
            string,
          ]
        }),
      }
    }

    case 'subAgentActivity':
      if (!finished) return null
      return {
        ...base,
        kind: 'subagent',
        title: 'delegated to a sub-agent',
        body: text(item?.agentPath),
        facts: [['kind', text(item?.kind)]],
      }

    case 'collabAgentToolCall':
      if (!finished) return null
      return {
        ...base,
        kind: 'collab',
        title: `called ${text(item?.tool) || 'a collaborator'}`,
        body: text(item?.prompt),
        facts: [['model', text(item?.model)], ['status', text(item?.status)]].filter(
          ([, v]) => v,
        ) as Array<[string, string]>,
      }

    default:
      // An approval request, or a method this page has never seen. Named
      // rather than dropped: an unknown row is information about the run.
      if (method.includes('requestApproval')) {
        const request = params as Record<string, unknown>
        return {
          ...base,
          kind: 'approval',
          title: 'asked for approval',
          body: text(request?.command) || text(request?.reason),
          facts: [['reason', text(request?.reason)], ['cwd', text(request?.cwd)]].filter(
            ([, v]) => v,
          ) as Array<[string, string]>,
        }
      }
      if (!finished && method.endsWith('/started')) return null
      // The item type names the thing; the method is `item/completed` for
      // every one of them and so says nothing on its own.
      return {
        ...base,
        kind: 'other',
        title: itemType || method || 'runtime event',
        body: '',
        facts: method && itemType ? [['method', method]] : [],
      }
  }
}

function isTruncated(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && (value as { truncated?: unknown }).truncated)
}

function summaryOf(value: unknown): string {
  if (Array.isArray(value)) return value.map((part) => text(part)).filter(Boolean).join('\n\n')
  return text(value)
}

function text(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.map(text).filter(Boolean).join(', ')
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    for (const key of ['text', 'content', 'summary', 'message', 'path']) {
      if (typeof record[key] === 'string') return record[key] as string
    }
    return JSON.stringify(value)
  }
  return String(value)
}

function handoffLabel(kind: string): string {
  return ({
    Brief: '작업 전달', Plan: '계획 완료', SourcePacket: '자료 수집 완료',
    ChangeAnalysis: '차이 분석 완료', ArchitectureDecision: '구조 결정 완료',
    ContractDraft: '계약 초안 완료', ContractReview: '계약 검토 완료',
    StepResult: '실행 결과 전달', Verdict: '판정 완료', Report: '보고 완료',
  } as Record<string, string>)[kind] ?? `${kind} 전달`
}

function summaryFromActivities(entries: Entry[]): string {
  const first = entries.find((entry) => entry.kind === 'command' || entry.kind === 'files' || entry.kind === 'handoff')
  if (!first) return ''
  if (first.kind === 'handoff') return '다음 단계에 다이제스트를 전달했습니다.'
  return first.title
}

/** Which agents appeared, in the order they first did. */
export function agentsOf(turns: Turn[]): string[] {
  const seen: string[] = []
  for (const turn of turns) if (!seen.includes(turn.agent)) seen.push(turn.agent)
  return seen
}
