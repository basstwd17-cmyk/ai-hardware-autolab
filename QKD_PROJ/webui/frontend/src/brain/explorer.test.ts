import { describe, expect, it } from 'vitest'
import { foldKnowledgeEvents, type KnowledgeEvent } from './model'
import {
  buildExplorerGraph,
  connectionsForNode,
  forceExplorerLayout,
  localExplorerGraph,
  matchingExplorerNodeIds,
} from './explorer'

function event(id: number, subject: string, object: string): KnowledgeEvent {
  return {
    event_id: id,
    at: `t${id}`,
    kind: 'knowledge.verified',
    claim_id: `claim-${id}`,
    version_id: `version-${id}`,
    payload: {
      status: 'verified',
      claim_id: `claim-${id}`,
      version_id: `version-${id}`,
      candidate: {
        producer: 'measurement',
        scope: { project_id: 'qkd-proj', hardware_id: 'rtase-v1' },
        subject: { kind: 'component', id: subject, label: subject.toUpperCase() },
        relation: 'connects_to',
        object: { kind: 'entity', entity: { kind: 'component', id: object, label: object.toUpperCase() } },
        statement: `${subject} connects to ${object}`,
        evidence: [],
        source_job_id: 'job-1',
        tags: ['hardware'],
      },
    },
  }
}

describe('Obsidian knowledge explorer', () => {
  it('builds a quiet concept network and a deeper evidence network', () => {
    const source = foldKnowledgeEvents([event(1, 'bbb', 'cpld')])
    const concepts = buildExplorerGraph(source, 'concepts')
    const evidence = buildExplorerGraph(source, 'evidence')
    expect(concepts.nodes.map((node) => node.label).sort()).toEqual(['BBB', 'CPLD'])
    expect(evidence.nodes.some((node) => node.kind === 'claim')).toBe(true)
    expect(evidence.nodes.find((node) => node.kind === 'job')?.claimIds).toContain('claim-1')
  })

  it('creates an undirected local graph with a bounded depth', () => {
    const graph = buildExplorerGraph(
      foldKnowledgeEvents([event(1, 'bbb', 'cpld'), event(2, 'cpld', 'dac')]),
      'concepts',
    )
    expect(localExplorerGraph(graph, 'entity:component:bbb', 1).nodes).toHaveLength(2)
    expect(localExplorerGraph(graph, 'entity:component:bbb', 2).nodes).toHaveLength(3)
  })

  it('searches note content and exposes readable backlinks', () => {
    const graph = buildExplorerGraph(foldKnowledgeEvents([event(1, 'bbb', 'cpld')]), 'concepts')
    expect(matchingExplorerNodeIds(graph, 'hardware')).toEqual(
      new Set(['entity:component:bbb', 'entity:component:cpld']),
    )
    const links = connectionsForNode(graph, 'entity:component:cpld')
    expect(links).toMatchObject([{ direction: 'in', peer: { label: 'BBB' }, edge: { label: 'connects to' } }])
  })

  it('starts from a deterministic force layout', () => {
    const graph = buildExplorerGraph(foldKnowledgeEvents([event(1, 'bbb', 'cpld')]), 'concepts')
    expect([...forceExplorerLayout(graph)]).toEqual([...forceExplorerLayout(graph)])
    expect(forceExplorerLayout(graph).get('entity:component:bbb')).not.toEqual(
      forceExplorerLayout(graph).get('entity:component:cpld'),
    )
  })
})
