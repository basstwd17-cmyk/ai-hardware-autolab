/**
 * Every accepted preflight enters the mission front door. A reviewed contract
 * is pinned as the baseline; it no longer skips change analysis.
 */

import { describe, expect, it } from 'vitest'
import { actionFor, scenarioWithScope, toneFor, type Preflight } from './discovery'

const SCENARIO = 'gate12 digital timing: observe SYM_A0, SYM_A1, D1CLK with CN1 disconnected'
const SHA = 'a'.repeat(64)

const READY: Preflight = {
  status: 'ready',
  scenario: SCENARIO,
  objective: SCENARIO,
  scenario_sha256: SHA,
  selected_contract: { name: 'gate12-pru-dac1-plan.yaml', sha256: 'c'.repeat(64) },
  execution_mode: 'hardware',
  approval_required: true,
}

const GAP: Preflight = {
  status: 'contract_gap',
  reason_code: 'no_contract_match',
  detail: 'No reviewed contract matches the scenario.',
  scenario: SCENARIO,
  objective: SCENARIO,
  scenario_sha256: SHA,
  selected_contract: null,
  candidates: [{ name: 'gate1-test-spec.yaml', status: 'blocked_pending_human_review', score: 1.2 }],
}

const OVERLAP: Preflight = {
  ...GAP,
  reason_code: 'ambiguous_contract',
  detail: 'Several reviewed contracts overlap.',
  candidates: [
    { name: 'bbb-dma-a.yaml', status: 'reviewed', score: 8 },
    { name: 'bbb-dma-b.yaml', status: 'reviewed', score: 8 },
  ],
}

describe('execution scope', () => {
  it('is part of the exact scenario sent to preflight', () => {
    expect(scenarioWithScope('  implement the host tool  ', 'offline')).toBe(
      'Execution scope: offline; no hardware access is permitted.\n\nimplement the host tool',
    )
    expect(scenarioWithScope('measure RF1', 'hardware')).toContain(
      'on board measurement or programming may be requested, subject to approval.',
    )
  })
})

describe('ready', () => {
  it('sends job.mission with the scenario digest and research policy', () => {
    const action = actionFor(READY, 'cmd-1')
    expect(action.kind).toBe('mission')
    expect(action.kind === 'mission' && action.frame).toEqual({
      type: 'job.mission',
      commandId: 'cmd-1',
      objective: SCENARIO,
      scenario: SCENARIO,
      scenarioSha256: SHA,
      searchPolicy: 'refresh',
    })
  })

  it('can preserve the operator choice to search every question', () => {
    const action = actionFor(READY, 'cmd-1', 'always')
    expect(action.kind === 'mission' && action.frame.searchPolicy).toBe('always')
  })

  it('offers nothing when the scenario digest is missing', () => {
    const halfway = { ...READY, scenario_sha256: undefined }
    const action = actionFor(halfway, 'cmd-1')
    expect(action.kind).toBe('none')
    expect(action.why).toContain('scenario digest')
  })
})

describe('contract_gap', () => {
  it('offers a research mission rather than reading as a failure', () => {
    const action = actionFor(GAP, 'cmd-2')
    expect(action.kind).toBe('mission')
    expect(action.label).toBe('조사·계획 시작')
    expect(toneFor('contract_gap')).toBe('open')
    expect(toneFor('contract_gap')).not.toBe('unclear')
  })

  it('sends job.mission with the scenario digest copied through exactly', () => {
    const action = actionFor(GAP, 'cmd-2')
    expect(action.kind === 'mission' && action.frame).toEqual({
      type: 'job.mission',
      commandId: 'cmd-2',
      objective: SCENARIO,
      scenario: SCENARIO,
      scenarioSha256: SHA,
      searchPolicy: 'refresh',
    })
    // The daemon re-derives it and refuses a mismatch, so it must be verbatim.
    const again = actionFor(GAP, 'cmd-2')
    expect(again.kind === 'mission' && again.frame.scenarioSha256).toBe(GAP.scenario_sha256)
  })

  it('offers nothing without a scenario digest, which discovery is keyed on', () => {
    const action = actionFor({ ...GAP, scenario_sha256: undefined }, 'cmd-2')
    expect(action.kind).toBe('none')
    expect(action.why).toContain('scenario digest')
  })

  it('routes overlapping contracts into one automatic derived contract', () => {
    const action = actionFor(OVERLAP, 'cmd-overlap')
    expect(action.kind).toBe('mission')
    expect(action.kind === 'mission' && action.label).toBe('파생 계약·계획 시작')
    expect(action.why).toContain('2 overlapping contracts become parents')
    expect(action.why).not.toContain('choose')
  })
})

describe('anything else', () => {
  it('says what came back rather than guessing between two doors', () => {
    for (const status of ['ambiguous', 'undetermined', 'something_new']) {
      const action = actionFor({ status, scenario: SCENARIO, scenario_sha256: SHA }, 'c')
      expect(action.kind).toBe('none')
      expect(action.why).toContain(status)
      expect(toneFor(status)).toBe('unclear')
    }
  })

  it('offers nothing before a preflight has been run', () => {
    expect(actionFor(null, 'c').kind).toBe('none')
  })

  it('falls back to the scenario when no separate objective came back', () => {
    const action = actionFor({ ...GAP, objective: undefined }, 'c')
    expect(action.kind === 'mission' && action.frame.objective).toBe(SCENARIO)
  })
})
