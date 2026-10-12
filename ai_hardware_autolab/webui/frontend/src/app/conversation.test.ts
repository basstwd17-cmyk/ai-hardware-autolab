/**
 * Ledger rows read as a conversation.
 *
 * The payload shapes below are taken from the captured App Server traffic in
 * `agent-platform/spikes/app-server/captures/`, not invented: `agentMessage`
 * carries `item.text`, `commandExecution` carries `item.command` and
 * `item.exitCode`, `fileChange` carries `item.changes`. A test written against
 * guessed field names would pass while the real UI rendered blanks.
 */

import { describe, expect, it } from 'vitest'
import type { LedgerEvent } from '../api/client'
import { agentsOf, mergeConversationRows, toFeedSteps, toLiveNotes, toTurns } from './conversation'

let nextId = 0

it('shows direct conversation replies and closes activity without legacy handoffs', () => {
  const rows = [
    event('conversation.turn.started', {}, 'lead'),
    event('conversation.turn.completed', { reply: { status: 'ready', summary: '기존 코드로 구현하겠습니다.' } }, 'lead'),
    event('conversation.feedback', { message: '출력은 정수로 해줘', by: 'local-operator' }, 'local-operator'),
  ]
  const turns = toTurns(rows)
  expect(turns[0].closedBy?.kind).toBe('Conversation')
  expect(toFeedSteps(turns)[0].summary).toBe('기존 코드로 구현하겠습니다.')
  expect(turns[1].agent).toBe('human')
  const notes = toLiveNotes(rows)
  expect(notes.some(note => note.text.includes('기존 코드'))).toBe(true)
  expect(notes.every(note => !note.current)).toBe(true)
})
function event(
  kind: string,
  payload: Record<string, unknown>,
  agent: string | null = null,
): LedgerEvent {
  nextId += 1
  return {
    event_id: nextId,
    job_id: 'job_1',
    seq: nextId,
    at: '2026-08-27T00:00:00+00:00',
    kind,
    agent,
    payload,
  }
}

function runtime(itemType: string, item: Record<string, unknown>, agent: string) {
  return event(
    'runtime.item/completed',
    { method: 'item/completed', item_type: itemType, params: { item: { type: itemType, ...item } } },
    agent,
  )
}

function reset() {
  nextId = 0
}

it('shows preparation and validated execution as platform activity, not hardware success', () => {
  const rows = [event('plan.preparation.required', { reason: 'missing candidate' }, 'platform'),
    event('plan.prepared.selected', { validation_pending: true }, 'platform'),
    event('step.dispatch.started', { tool: 'measure_scope' }, 'platform')]
  expect(toLiveNotes(rows).map(n => n.text).join(' ')).toContain('준비 중')
  expect(toLiveNotes(rows).map(n => n.text).join(' ')).toContain('결과는 아직 확인되지')
  expect(toTurns(rows).flatMap(t => t.entries)).toHaveLength(3)
})

it('shows host-owned device execution and its preserved run', () => {
  reset()
  const notes = toLiveNotes([
    event('conversation.device.started', { tool: 'measure_scope' }, 'platform'),
    event('conversation.device.completed', {
      tool: 'measure_scope', run: '20260910T010203Z_scope', run_verdict: 'PASS',
    }, 'platform'),
  ])
  expect(notes.map(note => note.text)).toEqual([
    '장비 호출 중: measure_scope',
    '장비 호출 완료: measure_scope · PASS · 20260910T010203Z_scope',
  ])
  expect(notes.every(note => !note.current)).toBe(true)
  expect(toTurns([
    event('conversation.device.completed', { tool: 'measure_scope', run_verdict: 'PASS' }, 'platform'),
  ])[0].entries[0].body).toContain('장비 호출 완료')
})

describe('turns', () => {
  it('starts with the person who opened the job', () => {
    reset()
    const turns = toTurns([
      event('job.created', { gate: 'gate12.yaml', objective: 'prove the DAC path' }),
    ])
    expect(turns).toHaveLength(1)
    expect(turns[0].agent).toBe('human')
    expect(turns[0].entries[0].kind).toBe('brief')
    expect(turns[0].entries[0].body).toBe('prove the DAC path')
    expect(turns[0].entries[0].facts).toContainEqual(['contract', 'gate12.yaml'])
  })

  it('breaks at a handoff, so a second turn by the same agent is its own', () => {
    reset()
    const turns = toTurns([
      runtime('agentMessage', { text: 'planning' }, 'gate-architect'),
      event('handoff.Plan', { sha256: 'a'.repeat(64), step_id: '' }, 'gate-architect'),
      runtime('agentMessage', { text: 'replanning' }, 'gate-architect'),
    ])
    expect(turns).toHaveLength(2)
    expect(turns[0].closedBy).toEqual({ kind: 'Plan', sha256: 'a'.repeat(64), stepId: '' })
    expect(turns[1].closedBy).toBeNull()
  })

  it('breaks when the agent changes', () => {
    reset()
    const turns = toTurns([
      runtime('agentMessage', { text: 'one' }, 'design-builder'),
      runtime('agentMessage', { text: 'two' }, 'verifier'),
    ])
    expect(turns.map((t) => t.agent)).toEqual(['design-builder', 'verifier'])
  })

  it('carries the iteration a row was written under', () => {
    reset()
    const turns = toTurns([
      event('iteration.started', { iteration: 3 }),
      runtime('agentMessage', { text: 'go' }, 'verifier'),
    ])
    expect(turns.at(-1)?.iteration).toBe(3)
  })

  it('lists the agents in the order they first appear', () => {
    reset()
    const turns = toTurns([
      runtime('agentMessage', { text: 'a' }, 'gate-architect'),
      runtime('agentMessage', { text: 'b' }, 'verifier'),
      runtime('agentMessage', { text: 'c' }, 'gate-architect'),
    ])
    expect(agentsOf(turns)).toEqual(['gate-architect', 'verifier'])
  })
})

describe('durable research progress', () => {
  it('renders a concise live note and keeps usage in the transcript', () => {
    reset()
    const row = event('research.progress', {
      queries_used: 3,
      new_fact_hashes: ['a'.repeat(64), 'b'.repeat(64)],
      tokens: { input: 1200, output: 180 },
      elapsed_s: 12.5,
    }, 'lead-agent')
    expect(toLiveNotes([row])[0].text).toContain('조사 3회')
    const entry = toTurns([row])[0].entries[0]
    expect(entry.title).toBe('조사 진행 기록')
    expect(entry.body).toContain('2개 근거')
    expect(entry.facts).toContainEqual(['입력 토큰', '1200'])
  })
})

describe('what each row becomes', () => {
  it('renders a command with its exit code and output', () => {
    reset()
    const [turn] = toTurns([
      runtime(
        'commandExecution',
        {
          command: 'py -3 -m pytest',
          cwd: 'C:/KIST',
          exitCode: 1,
          status: 'completed',
          aggregatedOutput: '1 failed',
        },
        'verifier',
      ),
    ])
    const entry = turn.entries[0]
    expect(entry.kind).toBe('command')
    expect(entry.body).toBe('py -3 -m pytest')
    expect(entry.facts).toContainEqual(['exit', '1'])
    expect(entry.facts).toContainEqual(['output', '1 failed'])
  })

  it('keeps an exit code of zero, which is the one that matters most', () => {
    reset()
    const [turn] = toTurns([
      runtime('commandExecution', { command: 'ls', exitCode: 0, status: 'completed' }, 'v'),
    ])
    // `0` is falsy: a filter written the obvious way drops exactly the result
    // that says the command succeeded.
    expect(turn.entries[0].facts).toContainEqual(['exit', '0'])
  })

  it('counts file changes and names them', () => {
    reset()
    const [turn] = toTurns([
      runtime(
        'fileChange',
        { status: 'completed', changes: [{ path: 'a.py', kind: 'edit' }, { path: 'b.py' }] },
        'design-builder',
      ),
    ])
    expect(turn.entries[0].title).toBe('changed 2 files')
    expect(turn.entries[0].facts[0]).toEqual(['a.py', 'edit'])
  })

  it('ignores the start of an item it will see completed', () => {
    reset()
    const started = event(
      'runtime.item/started',
      {
        method: 'item/started',
        item_type: 'agentMessage',
        params: { item: { type: 'agentMessage', text: '' } },
      },
      'verifier',
    )
    const turns = toTurns([started, runtime('agentMessage', { text: 'done' }, 'verifier')])
    expect(turns[0].entries).toHaveLength(1)
    expect(turns[0].entries[0].body).toBe('done')
  })

  it('names a runtime method it has never seen rather than dropping it', () => {
    reset()
    const turns = toTurns([
      event(
        'runtime.item/completed',
        { method: 'item/completed', item_type: 'somethingNew', params: { item: {} } },
        'verifier',
      ),
    ])
    expect(turns[0].entries[0].title).toBe('somethingNew')
  })

  it('surfaces an approval request with the command it is asking about', () => {
    reset()
    const turns = toTurns([
      event(
        'runtime.item/commandExecution/requestApproval',
        {
          method: 'item/commandExecution/requestApproval',
          params: { command: 'program_cpld_sram', reason: 'touches the board', cwd: 'C:/KIST' },
        },
        'board-operator',
      ),
    ])
    expect(turns[0].entries[0].kind).toBe('approval')
    expect(turns[0].entries[0].body).toBe('program_cpld_sram')
  })

  it('keeps operator feedback in the durable conversation as a human message', () => {
    reset()
    const [turn] = toTurns([
      event(
        'control.steer',
        { message: '계약 수정안을 먼저 작성해.', by: 'local-operator' },
        'local-operator',
      ),
    ])
    expect(turn.agent).toBe('human')
    expect(turn.entries[0].kind).toBe('message')
    expect(turn.entries[0].title).toBe('진행 방향 피드백')
    expect(turn.entries[0].body).toBe('계약 수정안을 먼저 작성해.')
    expect(turn.entries[0].facts).toContainEqual(['보낸 사람', 'local-operator'])
  })
})

describe('compact feed', () => {
  it('makes one readable card while retaining every source entry for details', () => {
    reset()
    const turns = toTurns([
      runtime('agentMessage', { text: 'I am checking the constraints.' }, 'verifier'),
      runtime('commandExecution', { command: 'py -3 -m pytest', exitCode: 0 }, 'verifier'),
      event('handoff.Verdict', { sha256: 'a'.repeat(64), step_id: 'verify' }, 'verifier'),
    ])
    const [step] = toFeedSteps(turns)
    expect(step.title).toBe('판정 완료')
    expect(step.summary).toBe('I am checking the constraints.')
    expect(step.chips).toContain('명령 1')
    expect(step.entries).toHaveLength(3)
  })
})

describe('live activity rail', () => {
  it('restores meaningful notes with an empty socket tail and ignores token noise', () => {
    reset()
    const history = [runtime('commandExecution', { command: 'Get-Content top.v', exitCode: 0 }, 'builder')]
    const noise = Array.from({ length: 25 }, () => event('runtime.account/rateLimits/updated', {}))
    const notes = toLiveNotes(mergeConversationRows(history, noise, 'job_1'))
    expect(notes).toHaveLength(1)
    expect(notes[0].text).toContain('top.v')
    expect(notes[0].source).toBe('activity')
    expect(toLiveNotes(mergeConversationRows([...history, ...noise], [], 'job_1'))).toEqual(notes)
  })

  it('deduplicates replay, excludes the previous job and clears completed activity', () => {
    reset()
    const start = event('runtime.item/started', { method: 'item/started', item_type: 'commandExecution', params: { item: { id: 'cmd1', command: 'pytest' } } }, 'builder')
    const finish = runtime('commandExecution', { id: 'cmd1', command: 'pytest', exitCode: 1 }, 'builder')
    const other = { ...event('job.created', {}), job_id: 'other' }
    const notes = toLiveNotes(mergeConversationRows([other, finish, start], [start, finish], 'job_1'))
    expect(notes).toHaveLength(2)
    expect(notes.every((note) => !note.current)).toBe(true)
    expect(notes[1].text).toContain('실패 (종료 코드 1)')
    expect(notes[1].text).not.toContain('다시')
  })

  it('shows messages but does not turn private reasoning into speech or invent clipped success', () => {
    reset()
    const notes = toLiveNotes([
      runtime('reasoning', { summary: 'internal-only text' }, 'builder'),
      runtime('agentMessage', { text: '핀 설정을 확인했습니다. 코드를 작성하겠습니다.' }, 'builder'),
      event('runtime.item/completed', { item_type: 'commandExecution', params: { truncated: true, head: 'partial', bytes: 10000 } }, 'builder'),
      event('iteration.started', { iteration: 1 }),
    ])
    expect(notes).toHaveLength(3)
    expect(notes[0]).toMatchObject({ source: 'message', text: '핀 설정을 확인했습니다. 코드를 작성하겠습니다.' })
    expect(notes[1].text).toContain('기록이 잘려')
    expect(notes[2].text).toBe('1번째 반복을 시작했습니다.')
  })

  it('keeps prose short and includes observed search query and file changes', () => {
    reset()
    const notes = toLiveNotes([
      runtime('webSearch', { action: { query: 'AM335x PRU timing' } }, 'lead-agent'),
      runtime('fileChange', { changes: [{ path: 'top.v' }] }, 'builder'),
      runtime('agentMessage', { text: 'x'.repeat(1000) }, 'builder'),
    ])
    expect(notes[0].text).toContain('AM335x PRU timing')
    expect(notes[1].text).toContain('1개 파일')
    expect(notes[2].text.length).toBeLessThanOrEqual(240)
  })

  it('turns observed search and collection events into short operator messages', () => {
    reset()
    const notes = toLiveNotes([
      event(
        'runtime.item/started',
        { method: 'item/started', item_type: 'webSearch', params: { item: { type: 'webSearch' } } },
        'researcher',
      ),
      event(
        'runtime.item/completed',
        {
          method: 'item/completed',
          item_type: 'dynamicToolCall',
          params: { item: { type: 'dynamicToolCall', tool: 'collect_source', status: 'failed', contentItems: [{ text: 'host could not be resolved' }] } },
        },
        'researcher',
      ),
    ])
    expect(notes.map((note) => note.text)).toEqual([
      '웹에서 자료를 검색하고 있습니다.',
      '도구 실행이 실패했습니다: collect_source',
    ])
    expect(notes[0].current).toBe(true)
  })

  it('reports a newly recorded operator steer without inventing delivery', () => {
    reset()
    const notes = toLiveNotes([
      event('control.steer', { message: 'RF1부터 확인해.', by: 'local-operator' }, 'local-operator'),
    ])
    expect(notes[0].agent).toBe('human')
    expect(notes[0].text).toContain('피드백')
    expect(notes[0].text).not.toContain('전달했습니다')
  })

  it('shows a recorded clarification as waiting for an answer, not a scheduler failure', () => {
    reset()
    const notes = toLiveNotes([event('job.blocked', { reason: 'user_input: 프로브 배율을 알려주세요.' })])
    expect(notes[0].text).toBe('답변을 기다립니다: 프로브 배율을 알려주세요.')
  })
})

it('does not call a completed platform event waiting for a record', () => {
  reset()
  const [step] = toFeedSteps(toTurns([
    event('job.done', { outcome: 'contract_gap', reason: 'research budget exhausted' }),
  ]))
  expect(step.title).toBe('작업 종료 기록')
  expect(step.summary).toBe('contract_gap — research budget exhausted')
})

describe('what it refuses to claim', () => {
  it('shows a handoff as a digest, because that is all the stream carries', () => {
    reset()
    const [turn] = toTurns([
      event('handoff.Verdict', { sha256: 'b'.repeat(64), step_id: 's1', iteration: 2 }, 'verifier'),
    ])
    const entry = turn.entries[0]
    expect(entry.title).toBe('handed off a Verdict')
    expect(entry.body).toBe('') // no invented prose
    expect(entry.facts).toContainEqual(['sha256', 'b'.repeat(64)])
    expect(entry.facts).toContainEqual(['step', 's1'])
  })

  it('says a payload was clipped instead of presenting the head as the whole', () => {
    reset()
    const [turn] = toTurns([
      event(
        'runtime.item/completed',
        {
          method: 'item/completed',
          item_type: 'commandExecution',
          params: { truncated: true, bytes: 41231, head: 'the first half of it' },
        },
        'verifier',
      ),
    ])
    expect(turn.entries[0].truncated).toBe(true)
    expect(turn.entries[0].body).toBe('the first half of it')
    expect(turn.entries[0].facts).toContainEqual(['clipped at', '41231 bytes'])
  })

  it('renders job.done as a marker and nothing more', () => {
    reset()
    const [turn] = toTurns([event('job.done', { outcome: 'pass' })])
    // Deciding the job is finished is the control plane's answer. This is a
    // row that was written, shown as a row that was written.
    expect(turn.entries[0].kind).toBe('marker')
    expect(turn.entries[0].title).toBe('job.done')
    expect(turn.agent).toBe('platform')
  })
})
