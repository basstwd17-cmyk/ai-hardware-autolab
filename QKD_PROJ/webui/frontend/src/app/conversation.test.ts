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
import { agentsOf, toFeedSteps, toTurns } from './conversation'

let nextId = 0
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
