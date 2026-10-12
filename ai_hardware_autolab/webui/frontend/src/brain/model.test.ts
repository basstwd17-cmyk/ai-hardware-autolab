import { describe, expect, it } from 'vitest'
import { compactBrainGraph, conceptLabel, foldKnowledgeEvents, matchesBrainQuery, type KnowledgeEvent } from './model'

function event(id: number, status = 'verified'): KnowledgeEvent {
  return {
    event_id: id, at: `t${id}`, kind: `knowledge.${status}`, claim_id: 'claim-a', version_id: `v${id}`,
    payload: {
      kind: 'KnowledgeClaim', claim_id: 'claim-a', version_id: `v${id}`, status,
      candidate: {
        producer: 'researcher', scope: { project_id: 'qkd-proj' },
        subject: { kind: 'component', id: 'pru', label: 'PRU' }, relation: 'has_interval',
        object: { kind: 'literal', value: 5, unit: 'ns' }, statement: 'PRU interval is 5 ns.',
        evidence: [{ kind: 'document_page', role: 'supports', locator: 'd'.repeat(64), sha256: 'd'.repeat(64), page: 42 }],
        source_job_id: 'job_1', tags: ['timing'],
      }, checks: [],
    },
  }
}

describe('brain projection', () => {
  it('builds entity, claim, literal, evidence and job relationships', () => {
    const graph = foldKnowledgeEvents([event(1)])
    expect(graph.nodes.map((node) => node.kind).sort()).toEqual(['claim', 'entity', 'evidence', 'job', 'literal'])
    expect(graph.edges.map((edge) => edge.label).sort()).toEqual(['has_interval', 'object', 'produced', 'supports'])
    expect(matchesBrainQuery(graph.claims[0], '5 ns')).toBe(true)
  })

  it('does not let a later proposal downgrade verified knowledge', () => {
    const graph = foldKnowledgeEvents([event(1), event(2, 'proposed'), event(1)])
    expect(graph.claims[0].status).toBe('verified')
    expect(graph.cursor).toBe(2)
  })

  it('marks an existing claim superseded', () => {
    const superseded: KnowledgeEvent = {
      event_id: 2, at: 't2', kind: 'knowledge.superseded', claim_id: 'claim-a', version_id: 's',
      payload: { old_claim_id: 'claim-a', new_claim_id: 'claim-b', reason: 'new evidence' },
    }
    expect(foldKnowledgeEvents([event(1), superseded]).claims[0].status).toBe('superseded')
  })

  it('projects the default view as short concepts connected by the claim relation', () => {
    const graph = compactBrainGraph(foldKnowledgeEvents([event(1)]))
    expect(graph.nodes.map((node) => node.label).sort()).toEqual(['5 ns', 'PRU'])
    expect(graph.edges).toMatchObject([
      { label: 'has interval', status: 'verified', claimId: 'claim-a' },
    ])
    expect(conceptLabel('a long hardware timing relationship with extra words')).toBe(
      'a long hardware timing relationship…',
    )
  })
})
