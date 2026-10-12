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

/** One short, operator-facing sentence for the live activity rail. */
export interface LiveNote {
  eventId: number
  at: string
  agent: string
  text: string
  current: boolean
  source: 'message' | 'activity'
}

const HANDOFF = 'handoff.'
const RUNTIME = 'runtime.'

/** The agent a row belongs to; `human` for rows a person caused. */
function actorOf(event: LedgerEvent): string {
  if (event.kind === 'control.steer' || event.kind === 'conversation.feedback') return 'human'
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

    if (event.kind === 'conversation.turn.completed') {
      turn.closedBy = { kind: 'Conversation', sha256: '', stepId: '' }
    } else if (event.kind.startsWith(HANDOFF)) {
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
    const marker = turn.entries.filter((entry) => entry.kind === 'marker').at(-1)
    const narrative = [...visible].reverse().find((entry) => (
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
          : marker
            ? markerLabel(marker.title)
            : '기록 없음'
    return {
      agent: turn.agent,
      iteration: turn.iteration,
      entries: turn.entries,
      closedBy: turn.closedBy,
      title,
      summary: (narrative?.body ?? summaryFromActivities(visible)) || markerSummary(marker),
      chips,
      at: turn.entries[0]?.at ?? '',
    }
  })
}

function markerLabel(kind: string): string {
  return ({
    'job.done': '작업 종료 기록',
    'job.blocked': '작업 중단 기록',
    'job.running': '작업 재개 기록',
    'iteration.started': '반복 시작 기록',
  } as Record<string, string>)[kind] ?? kind
}

function markerSummary(marker: Entry | undefined): string {
  if (!marker) return ''
  const facts = new Map(marker.facts)
  const outcome = facts.get('outcome')
  const reason = facts.get('reason')
  if (outcome && reason) return `${outcome} — ${reason}`
  return outcome ?? reason ?? ''
}

/**
 * The durable transcript is intentionally complete and verbose.  This is its
 * complementary view: a small messenger-like explanation of the latest
 * actions.  It only translates observed event kinds; it never invents a
 * conclusion about the job or hides the raw events available below.
 */
export function toLiveNotes(events: LedgerEvent[]): LiveNote[] {
  const notes: LiveNote[] = []
  const pending = new Map<string, LiveNote>()
  for (const event of mergeConversationRows(events, [])) {
    const payload = event.payload as { method?: string; params?: { threadId?: string; item?: { id?: string; type?: string } }; item_type?: string }
    const method = payload.method ?? event.kind.replace(/^runtime\./, '')
    const key = `${event.job_id}:${actorOf(event)}:${payload.params?.threadId ?? ''}:${payload.params?.item?.id ?? payload.item_type ?? payload.params?.item?.type ?? ''}`
    if (method.endsWith('/completed')) {
      const previous = pending.get(key)
      if (previous) previous.current = false
      pending.delete(key)
      if (isTruncated(payload.params)) {
        // A clipped completion loses the item id. Do not leave its start
        // looking active forever; the raw record still declares the gap.
        for (const [pendingKey, note] of pending) {
          if (note.agent === actorOf(event)) { note.current = false; pending.delete(pendingKey) }
        }
      }
    }
    if (event.kind.startsWith(HANDOFF) || event.kind === 'conversation.turn.completed' || event.kind === 'conversation.turn.failed' || event.kind === 'conversation.device.completed' || event.kind === 'conversation.device.failed' || event.kind === 'runtime.turn/completed' || event.kind === 'job.done' || event.kind === 'job.blocked') {
      for (const note of notes) if (event.kind.startsWith('job.') || note.agent === actorOf(event)) note.current = false
    }
    const text = liveText(event)
    if (!text) continue
    const note: LiveNote = {
      eventId: event.event_id,
      at: event.at,
      agent: actorOf(event),
      text: shortNote(text),
      current: method === 'item/started' || event.kind === 'conversation.turn.started' || event.kind === 'conversation.device.started',
      source: event.kind === 'control.steer' || event.kind === 'conversation.feedback' || event.kind === 'conversation.turn.completed' || payload.item_type === 'agentMessage' || payload.params?.item?.type === 'agentMessage' ? 'message' : 'activity',
    }
    notes.push(note)
    if (note.current) pending.set(key, note)
  }
  return notes.slice(-8)
}

/** Replay, websocket tail and tab remount all use the same durable feed. */
export function mergeConversationRows(history: LedgerEvent[], live: LedgerEvent[], jobId?: string | null): LedgerEvent[] {
  const rows = new Map<number, LedgerEvent>()
  for (const event of [...history, ...live]) {
    if (jobId !== undefined && event.job_id !== jobId) continue
    if (!rows.has(event.event_id)) rows.set(event.event_id, event)
  }
  return [...rows.values()].sort((a, b) => a.event_id - b.event_id)
}

function liveText(event: LedgerEvent): string {
  if (event.kind === 'conversation.turn.started') return '전달받은 목표와 결과를 확인하고 작업을 시작합니다.'
  if (event.kind === 'conversation.turn.completed') return text((event.payload as { reply?: { summary?: unknown } }).reply?.summary)
  if (event.kind === 'conversation.turn.failed') return `작업 응답을 기록하지 못했습니다: ${text((event.payload as { error?: unknown }).error)}`
  if (event.kind === 'conversation.feedback') return `피드백: ${text((event.payload as { message?: unknown }).message)}`
  if (event.kind === 'conversation.device.started') return `장비 호출 중: ${text((event.payload as { tool?: unknown }).tool) || '장비 도구'}`
  if (event.kind === 'conversation.device.completed') {
    const payload = event.payload as { tool?: unknown; run?: unknown; run_verdict?: unknown }
    const verdict = text(payload.run_verdict) || '결과 기록됨'
    const run = text(payload.run)
    return `장비 호출 완료: ${text(payload.tool) || '장비 도구'} · ${verdict}${run ? ` · ${run}` : ''}`
  }
  if (event.kind === 'conversation.device.failed') return `장비 결과를 확정하지 못했습니다: ${text((event.payload as { error?: unknown }).error)}`
  if (event.kind === 'plan.preparation.required') return '준비 중 · Builder가 기존 자료로 실행에 필요한 코드와 입력을 준비합니다. 아직 장비에 접근하지 않습니다.'
  if (event.kind === 'plan.prepared.selected') return 'Builder의 준비 결과를 이어받아 실행 전 검사를 진행합니다.'
  if (event.kind === 'step.dispatch.started') return '실행 중 · 장비 단계 실행을 시작합니다. 결과는 아직 확인되지 않았습니다.'
  if (event.kind === 'plan.validation_failed') return '수정 중 · 계획에서 보완할 내용을 확인했습니다.'
  if (event.kind === 'control.steer') return `피드백 기록: ${text((event.payload as { message?: unknown }).message)}`
  if (event.kind === 'job.created') return '시나리오를 받아 작업을 시작했습니다.'
  if (event.kind === 'iteration.started') {
    const iteration = (event.payload as { iteration?: unknown }).iteration
    return typeof iteration === 'number' ? `${iteration}번째 반복을 시작했습니다.` : '새 반복을 시작했습니다.'
  }
  if (event.kind.startsWith(HANDOFF)) return `${handoffLabel(event.kind.slice(HANDOFF.length))}을 다음 단계에 전달했습니다.`
  if (event.kind === 'job.done') return '작업 결과를 원장에 기록했습니다.'
  if (event.kind === 'job.blocked') {
    const reason = text((event.payload as { reason?: unknown }).reason)
    return reason.startsWith('user_input:')
      ? `답변을 기다립니다: ${reason.slice('user_input:'.length).trim()}`
      : `진행이 멈췄습니다: ${reason || '진행 조건 확인 필요'}`
  }
  if (event.kind === 'approval.pending') return '승인 요청을 기록했습니다. 사람의 결정을 기다립니다.'
  if (event.kind === 'research.progress') {
    const payload = event.payload as { queries_used?: unknown; new_fact_hashes?: unknown }
    const queries = typeof payload.queries_used === 'number' ? payload.queries_used : 0
    const facts = Array.isArray(payload.new_fact_hashes) ? payload.new_fact_hashes.length : 0
    return `조사 ${queries}회에서 새로운 근거 ${facts}개를 기록했습니다.`
  }
  if (!event.kind.startsWith(RUNTIME)) return ''

  const payload = event.payload as { method?: unknown; params?: unknown; item_type?: unknown }
  const method = text(payload.method) || event.kind.slice(RUNTIME.length)
  const params = payload.params as { item?: unknown } | undefined
  const item = params?.item as Record<string, unknown> | undefined
  const type = text(payload.item_type) || text(item?.type)
  const started = method.endsWith('/started')
  const finished = method.endsWith('/completed')

  if (type === 'reasoning') return '' // Internal reasoning is not agent-to-user speech.
  const query = text((item?.action as { query?: unknown } | undefined)?.query) || text(item?.query)
  const failed = item?.status === 'failed' || item?.success === false || !!item?.error
  if (started && type === 'webSearch') return query ? `웹 검색 중: ${query}` : '웹에서 자료를 검색하고 있습니다.'
  if (started && type === 'dynamicToolCall') {
    return text(item?.tool) === 'collect_source'
      ? '찾은 문서를 지식 기록으로 수집하고 있습니다.'
      : `도구 실행 중: ${text(item?.tool) || '도구 정보 수신 대기'}`
  }
  if (started && type === 'commandExecution') return commandNote(item, false)
  if (started && type === 'fileChange') return '코드 변경을 적용하고 있습니다.'
  if (started && type === 'mcpToolCall') return `장비·도구 호출 중: ${text(item?.tool) || 'MCP'}`
  if (!finished) return ''

  if (isTruncated(payload.params)) return `${({ commandExecution: '명령 실행', fileChange: '파일 변경', agentMessage: '에이전트 메시지', dynamicToolCall: '도구 호출', mcpToolCall: 'MCP 호출' } as Record<string, string>)[type] ?? '활동'} 종료 이벤트를 받았습니다. 기록이 잘려 상세 결과는 확인할 수 없습니다.`
  if (type === 'webSearch') return failed ? '웹 검색이 실패했습니다.' : `웹 검색을 마쳤습니다${query ? `: ${query}` : '.'}`
  if (type === 'dynamicToolCall') {
    if (failed) return `도구 실행이 실패했습니다: ${text(item?.tool) || '자료 수집'}`
    return `도구 실행을 마쳤습니다: ${text(item?.tool) || '조사 도구'}`
  }
  if (type === 'commandExecution') return commandNote(item, true)
  if (type === 'fileChange') return failed ? '코드 변경 적용이 실패했습니다.' : `파일 변경 결과를 받았습니다${Array.isArray(item?.changes) ? `: ${item.changes.length}개 파일` : '.'}`
  if (type === 'mcpToolCall') return `도구 ${text(item?.tool) || 'MCP'}: ${failed ? '실패' : '응답 수신'}`
  if (type === 'agentMessage') {
    const value = text(item?.text).trim()
    return /^[{[]/.test(value) ? '구조화된 응답을 작성했습니다. 본문은 아래 상세 기록에서 확인할 수 있습니다.' : value
  }
  return ''
}

function shortNote(value: string): string {
  const flat = value.replace(/\s+/g, ' ').trim()
  return flat.length > 240 ? `${flat.slice(0, 239)}…` : flat
}

function commandNote(item: Record<string, unknown> | undefined, finished: boolean): string {
  const command = text(item?.command)
  const label = /\b(pytest|vitest)\b|npm(?:\.cmd)? (?:run )?test/i.test(command) ? '테스트 명령'
    : /\b(cmake|make|msbuild)\b|npm(?:\.cmd)? run build/i.test(command) ? '빌드 명령'
      : /\b(rg|Select-String)\b/i.test(command) ? '파일 검색 명령'
        : /\b(Get-Content|cat|type)\b/i.test(command) ? '파일 읽기 명령' : '명령'
  const leaf = command.match(/(?:[\w.-]+\.(?:py|md|ts|tsx|c|cpp|h|v|sv|yaml|json|pdf))\b/i)?.[0]
  const target = leaf ? ` · ${leaf}` : ''
  if (!finished) return `${label} 실행 중${target}`
  const code = item?.exitCode
  if (typeof code === 'number' && code !== 0) return `${label} 실패 (종료 코드 ${code})${target}`
  if (item?.status === 'declined' || item?.status === 'failed') return `${label}: ${item.status === 'declined' ? '실행 거부' : '실패'}${target}`
  return `${label} 종료${code === 0 ? ' (종료 코드 0)' : ' (성공 여부 미확인)'}${target}`
}

function toEntry(event: LedgerEvent): Entry | null {
  const base = {
    eventId: event.event_id,
    at: event.at,
    facts: [] as Array<[string, string]>,
    truncated: false,
  }

  if (['conversation.turn.started', 'conversation.turn.completed', 'conversation.turn.failed', 'conversation.feedback', 'conversation.device.started', 'conversation.device.completed', 'conversation.device.failed'].includes(event.kind)) {
    return { ...base, kind: 'message', title: event.kind === 'conversation.turn.completed' ? '작업 전달' : '진행 메시지', body: liveText(event) }
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

  if (['plan.preparation.required', 'plan.prepared.selected', 'plan.validation_failed', 'step.dispatch.started'].includes(event.kind)) {
    return { ...base, kind: 'marker', title: liveText(event), body: text((event.payload as { reason?: unknown; error?: unknown }).reason ?? (event.payload as { error?: unknown }).error) }
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

  if (event.kind === 'control.steer') {
    const payload = event.payload as { message?: unknown; by?: unknown }
    return {
      ...base,
      kind: 'message',
      title: '진행 방향 피드백',
      body: text(payload.message),
      facts: text(payload.by) ? [['보낸 사람', text(payload.by)]] : [],
    }
  }

  if (event.kind === 'research.progress') {
    const payload = event.payload as Record<string, unknown>
    const tokens = payload.tokens as Record<string, unknown> | undefined
    return {
      ...base,
      kind: 'reasoning',
      title: '조사 진행 기록',
      body: `${text(payload.queries_used) || '0'}회 검색 · ${Array.isArray(payload.new_fact_hashes) ? payload.new_fact_hashes.length : 0}개 근거`,
      facts: [
        ['누적 제한 전 사용', text(payload.queries_used)],
        ['입력 토큰', text(tokens?.input)],
        ['출력 토큰', text(tokens?.output)],
        ['경과 시간(초)', text(payload.elapsed_s)],
      ].filter(([, value]) => value) as Array<[string, string]>,
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
    Conversation: '작업 전달',
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
