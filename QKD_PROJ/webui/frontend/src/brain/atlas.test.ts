import { describe, expect, it } from 'vitest'
import { buildAtlasGraph } from './atlas'
import { foldKnowledgeEvents, type KnowledgeEvent } from './model'

function event(
  id: number,
  subject: Record<string, unknown>,
  object: Record<string, unknown>,
  tags: string[] = [],
  evidence: Record<string, unknown>[] = [],
): KnowledgeEvent {
  return {
    event_id: id, at: '2026-08-31T00:00:00Z', kind: 'knowledge.proposed', claim_id: `claim-${id}`, version_id: `version-${id}`,
    payload: { status: 'proposed', claim_id: `claim-${id}`, version_id: `version-${id}`, candidate: {
      subject, relation: 'connects_to', object, statement: 'test', producer: 'researcher', scope: {}, evidence, tags,
    } },
  }
}

describe('knowledge atlas', () => {
  it('groups known board models beneath Board without inventing a board relation', () => {
    const graph = foldKnowledgeEvents([
      event(1, { kind: 'board', id: 'bbb', label: 'BeagleBone Black' }, { kind: 'entity', entity: { kind: 'controller', id: 'pru', label: 'PRU' } }),
    ])
    const atlas = buildAtlasGraph(graph)
    expect(atlas.nodes.find((node) => node.id === 'entity:board:bbb')?.category).toBe('board')
    expect(atlas.nodes.find((node) => node.id === 'entity:controller:pru')?.category).toBe('board')
    expect(atlas.edges.some((edge) => edge.id.includes('fact:concept:claim-1'))).toBe(true)
  })

  it('places cited material under Docs and keeps it distinct from hardware nodes', () => {
    const graph = foldKnowledgeEvents([
      event(
        2,
        { kind: 'question', id: 'pin-map', label: 'pin map' },
        { kind: 'literal', value: 'P9_21' },
        ['research'],
        [{ kind: 'document_page', role: 'primary', locator: 'doc', sha256: 'a'.repeat(64), page: 2 }],
      ),
    ])
    const atlas = buildAtlasGraph(graph)
    expect(atlas.nodes.find((node) => node.id.startsWith('atlas:evidence:'))?.category).toBe('document')
  })
})
