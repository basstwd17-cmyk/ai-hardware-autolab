import { describe, expect, it } from 'vitest'
import { buildTopicGraph, TOPIC_ROOT } from './topics'
import { foldKnowledgeEvents, type KnowledgeEvent } from './model'
import { BRANCH_PAGE_SIZE, mindMapLayout, searchTopicClaims, topicBranch, topicForClaim, topicPath } from './mindmap'

function source(count = 25) {
  return foldKnowledgeEvents(Array.from({length: count}, (_, i): KnowledgeEvent => ({
    event_id: i + 1, at: 'now', kind: 'knowledge.proposed', claim_id: `c${i}`, version_id: `v${i}`,
    payload: {candidate: {subject: {kind: 'board', id: 'new-board', label: 'New Board'},
      scope: {hardware_id: 'new-board', revision: 'r1'}, relation: 'supports', statement: `SPI timing record ${i}`,
      object: {kind: 'literal', value: i}, evidence: [], tags: []}},
  })))
}

describe('progressive knowledge mind map', () => {
  it('starts with only the center and broad categories, then reveals a board and its features', () => {
    const graph = buildTopicGraph(source())
    const home = topicBranch(graph, TOPIC_ROOT)
    expect(home.graph.nodes).toHaveLength(7)
    expect(home.graph.nodes.some(n => n.label === 'New Board · r1')).toBe(false)
    const board = topicBranch(graph, 'topic:board').graph.nodes.find(n => n.label === 'New Board · r1')!
    const feature = topicBranch(graph, board.id).graph.nodes.find(n => n.label === 'SPI')!
    expect(topicPath(graph, feature.id).map(n => n.label)).toEqual(['개발 지식', '보드', 'New Board · r1', 'SPI'])
    expect(topicBranch(graph, feature.id).graph.nodes.filter(n => n.kind === 'claim')).toHaveLength(BRANCH_PAGE_SIZE)
  })

  it('paginates all records exactly once and clamps a page after filtering', () => {
    const graph = buildTopicGraph(source())
    const parent = topicForClaim(graph, 'c0')!
    const first = topicBranch(graph, parent.id)
    const ids = new Set<string>()
    for (let page = 0; page < first.pages; page++) {
      const branch = topicBranch(graph, parent.id, page)
      branch.graph.nodes.filter(n => n.claimId).forEach(n => ids.add(n.claimId!))
      const positions = mindMapLayout(branch.graph)
      expect(new Set([...positions.values()].map(p => `${p.x}:${p.y}`)).size).toBe(branch.graph.nodes.length)
    }
    expect(ids.size).toBe(25)
    expect(topicBranch(graph, parent.id, 999).page).toBe(2)
    expect(topicBranch(graph, 'missing').focus.id).toBe(TOPIC_ROOT)
    const filtered = buildTopicGraph(source(1))
    expect(topicBranch(filtered, parent.id, 2).page).toBe(0)
  })

  it('searches collapsed branches and finds a path with the original status and scope', () => {
    const original = source()
    const record = original.claims.find(c => c.claimId === 'c24')!
    record.status = 'refuted'
    const graph = buildTopicGraph(original)
    expect(searchTopicClaims(graph, 'record 24')).toEqual([record])
    expect(searchTopicClaims(graph, 'new-board')).toHaveLength(25)
    const parent = topicForClaim(graph, 'c24')!
    expect(topicPath(graph, parent.id)[0].id).toBe(TOPIC_ROOT)
    expect(parent.id.startsWith('topic:board:')).toBe(true)
    expect(record.scope).toEqual({hardware_id: 'new-board', revision: 'r1'})
    const records = Array.from({length: topicBranch(graph, parent.id).pages}, (_, page) => topicBranch(graph, parent.id, page).graph.nodes).flat()
    expect(records.find(n => n.claimId === 'c24')?.status).toBe('refuted')
  })

  it('pages wide topic lists without losing boards after the first ten', () => {
    const original = source()
    original.claims.forEach((c, i) => { c.subject = {kind: 'board', id: `board-${i}`, label: `Board ${i}`} })
    const graph = buildTopicGraph(original)
    expect(topicBranch(graph, 'topic:board').total).toBe(25)
    expect(topicBranch(graph, 'topic:board', 2).graph.nodes).toHaveLength(6)
  })
})
