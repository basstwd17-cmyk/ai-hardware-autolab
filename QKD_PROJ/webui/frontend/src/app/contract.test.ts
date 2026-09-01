/**
 * A contract proposal, read without being promoted.
 *
 * The failure this guards against is a reading failure, not a crash: a badge
 * that says "ready" reads as "go", and an operator who reads it that way looks
 * for the run button. `ready_for_human_review` means "no review error or
 * required change is known" and the reviewer's own definition adds: not
 * approval, and never executable.
 */

import { describe, expect, it } from 'vitest'
import { findingsBySeverity, proposalOf, requiredChanges, standingFor } from './contract'

const REVIEW = {
  kind: 'ContractReview',
  agent: 'contract-reviewer',
  scenario_sha256: 'a'.repeat(64),
  draft_sha256: 'd'.repeat(64),
  outcome: 'ready_for_human_review',
  findings: [
    { code: 'bounded-scope', severity: 'info', detail: 'allowed_operations is bounded' },
    { code: 'no-source', severity: 'error', detail: 'pass criterion 2 cites no claim' },
    { code: 'wide-mode', severity: 'warning', detail: 'execution_mode is hardware' },
  ],
  required_changes: [],
}

const DRAFT = {
  kind: 'ContractDraft',
  agent: 'contract-drafter',
  contract_name: 'gate13-symbol-timing.yaml',
  architecture_id: 'arch-1',
  execution_mode: 'hardware',
  objective: 'observe symbol timing',
  roles: ['measurement'],
  steps: [{ id: 's1', agent: 'measurement', tool: 'measure_gate1_gpio' }],
}

const RESULT = {
  jobId: 'job_1',
  reasonCode: 'ready_for_human_review',
  contractDraft: DRAFT,
  contractDraftSha256: 'd'.repeat(64),
  contractReview: REVIEW,
  contractReviewSha256: 'r'.repeat(64),
}

describe('recognising a proposal', () => {
  it('reads the five fields off a result', () => {
    const proposal = proposalOf(RESULT)
    expect(proposal?.draftSha256).toBe('d'.repeat(64))
    expect(proposal?.reviewSha256).toBe('r'.repeat(64))
    expect(proposal?.reasonCode).toBe('ready_for_human_review')
    expect(proposal?.draft?.contract_name).toBe('gate13-symbol-timing.yaml')
    expect(proposal?.review?.findings).toHaveLength(3)
  })

  it('is null for an ordinary verdict result', () => {
    expect(proposalOf({ jobId: 'job_1', outcome: 'satisfied', summary: 'x' })).toBeNull()
    expect(proposalOf(undefined)).toBeNull()
  })

  it('recognises a review that arrived without its draft', () => {
    expect(proposalOf({ contractReview: REVIEW })?.draft).toBeNull()
  })
})

describe('what the badge is allowed to say', () => {
  it('never says approved or executable', () => {
    for (const code of ['ready_for_human_review', 'revise', 'rejected', 'something_new', null]) {
      const standing = standingFor(code)
      const words = `${standing.label} ${standing.caveat}`.toLowerCase()
      expect(words).not.toContain('approved')
      expect(words).not.toContain('executable.')
      expect(words).not.toMatch(/\bis executable\b/)
      expect(words).not.toMatch(/\bready to run\b/)
    }
  })

  it('renders ready_for_human_review as review-ready, with what it is not', () => {
    const standing = standingFor('ready_for_human_review')
    expect(standing.label).toBe('검토 준비됨')
    expect(standing.tone).toBe('open')
    expect(standing.caveat).toContain('not approval')
    expect(standing.caveat).toContain('a person still has to read and sign it')
  })

  it('shows an unknown reason code verbatim rather than mapping it', () => {
    const standing = standingFor('promoted')
    expect(standing.label).toBe('promoted')
    expect(standing.tone).toBe('unclear')
    expect(standing.caveat).toContain('not one this build knows')
  })

  it('claims nothing when no reason code came back', () => {
    expect(standingFor(null).caveat).toContain('nothing is claimed')
  })
})

describe('findings', () => {
  it('groups by severity, worst first', () => {
    expect(findingsBySeverity(REVIEW).map(([severity]) => severity)).toEqual([
      'error',
      'warning',
      'info',
    ])
  })

  it('puts a severity it has never seen last rather than dropping it', () => {
    const odd = { findings: [{ severity: 'catastrophe' }, { severity: 'info' }] }
    expect(findingsBySeverity(odd).map(([s]) => s)).toEqual(['info', 'catastrophe'])
  })

  it('renders required changes, including ones that are not strings', () => {
    expect(requiredChanges({ required_changes: ['drop step 2', { field: 'roles' }] })).toEqual([
      'drop step 2',
      '{"field":"roles"}',
    ])
    expect(requiredChanges(null)).toEqual([])
  })
})
