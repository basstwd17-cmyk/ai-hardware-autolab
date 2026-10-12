import { describe, it, expect } from 'vitest'
import { buildTopicGraph } from './topics'
import { foldKnowledgeEvents, type KnowledgeEvent } from './model'

function event(id: number, statement: string): KnowledgeEvent {
  return {event_id: id, at: 'now', kind: 'knowledge.proposed', claim_id: `c${id}`, version_id: `v${id}`,
    payload: {candidate: {producer: 'lead', subject: {id: `mission-${id}`, label: `Mission ${id}`, kind: 'decision'},
      relation: 'proposes', object: {kind: 'literal', value: statement}, statement, tags: [], evidence: []}}}
}
describe('technical topic navigation', () => {
  it('adds arbitrary new feature notes under their selected board without a keyword list', () => {
    const record = event(1, '새로운 광원 보정 방법')
    Object.assign((record.payload.candidate as Record<string,unknown>), {
      subject:{kind:'software',id:'laser-calibration',label:'광원 보정'},
      scope:{project_id:'p',hardware_id:'custom-board',revision:'r3'},
      relation:'work_note',tags:['conversation'],
    })
    const graph = buildTopicGraph(foldKnowledgeEvents([record]))
    const board = graph.nodes.find(n=>n.label==='custom-board · r3')!
    expect(graph.edges.some(e=>e.source===board.id && graph.nodes.find(n=>n.id===e.target)?.label==='광원 보정')).toBe(true)
  })
  it('groups stored board features and keeps bureaucratic nodes out of the default view', () => {
    const source = foldKnowledgeEvents([event(1, 'BBB PRU and DMA transfer'), event(2, 'STM32 HRTimer waveform')])
    const graph = buildTopicGraph(source)
    expect(graph.edges).toEqual(expect.arrayContaining([
      expect.objectContaining({source: 'topic:board', target: 'topic:board:BBB'}),
      expect.objectContaining({source: 'topic:board:BBB', target: 'topic:board:BBB:PRU'}),
      expect.objectContaining({source: 'topic:board:BBB', target: 'topic:board:BBB:DMA'}),
      expect.objectContaining({source: 'topic:board:STM32', target: 'topic:board:STM32:HRTIM'}),
    ]))
    expect(graph.nodes.some(n => /Mission|condition|contract/i.test(n.label))).toBe(false)
    expect(graph.nodes.find(n => n.label === 'PRU')?.claimIds).toEqual(['c1'])
    expect(graph.claims).toBe(source.claims)
    expect(graph.nodes.every(n => n.navigation && !n.status)).toBe(true)
  })
  it('shows only classification roots before evidence arrives, not fake learned boards', () => {
    const graph = buildTopicGraph(foldKnowledgeEvents([]))
    expect(graph.nodes.map(n => n.label)).toContain('보드')
    expect(graph.nodes.every(n => n.claimIds.length === 0)).toBe(true)
    expect(graph.nodes.some(n => n.label === 'BBB')).toBe(false)
  })
  it('does not assign an unrelated PRU fact to STM32 from another record', () => {
    const graph = buildTopicGraph(foldKnowledgeEvents([event(1, 'PRU'), event(2, 'STM32')]))
    expect(graph.nodes.some(n => n.id === 'topic:software:PRU')).toBe(true)
    expect(graph.nodes.some(n => n.id === 'topic:board:STM32:PRU')).toBe(false)
  })
  it('retains unknown board models and deduplicates repeated topic mentions', () => {
    const raw = event(1, 'custom timing board SPI')
    const candidate = raw.payload.candidate as Record<string, unknown>
    candidate.subject = {kind: 'board', id: 'custom-v2', label: 'Custom V2'}
    const graph = buildTopicGraph(foldKnowledgeEvents([raw, event(2, 'BBB BBB PRU PRU')]))
    expect(graph.nodes.some(n => n.label === 'Custom V2')).toBe(true)
    expect(graph.nodes.filter(n => n.id === 'topic:board:BBB:PRU')).toHaveLength(1)
    expect(new Set(graph.edges.map(e => e.id)).size).toBe(graph.edges.length)
  })
  it('separates old procedural records without losing their provenance or searchability', () => {
    const raw = event(1, 'BBB contract needs a compiler')
    ;(raw.payload.candidate as Record<string, unknown>).relation = 'requires_material'
    const source = foldKnowledgeEvents([raw, event(2, 'BBB PRU waveform')])
    const graph = buildTopicGraph(source)
    expect(graph.nodes.find(n => n.id === 'topic:board:BBB')?.claimIds).toEqual(['c2'])
    expect(graph.nodes.find(n => n.id === 'topic:document:history')?.claimIds).toEqual(['c1'])
    expect(graph.nodes.find(n => n.id === 'topic:root')?.claimIds).toEqual(['c1', 'c2'])
    expect(graph.claims).toBe(source.claims)
  })
  it('keeps arbitrary board scopes and revisions separate even without known board names', () => {
    const raws = [event(1, 'SPI timing is 10 ns'), event(2, 'SPI timing is 20 ns'), event(3, 'connector details')]
    raws.forEach((raw, i) => {
      ;(raw.payload.candidate as Record<string, unknown>).scope = {
        project_id: 'lab', hardware_id: 'new-board', revision: i === 1 ? 'r2' : 'r1',
      }
    })
    const graph = buildTopicGraph(foldKnowledgeEvents(raws))
    const boards = graph.nodes.filter(n => /^new-board · r[12]$/.test(n.label))
    expect(boards).toHaveLength(2)
    expect(boards.find(n => n.label.endsWith('r1'))?.claimIds).toEqual(['c1', 'c3'])
    expect(boards.find(n => n.label.endsWith('r2'))?.claimIds).toEqual(['c2'])
    const parentIds = new Set(graph.edges.map(e => e.source))
    const leaves = graph.nodes.filter(n => !parentIds.has(n.id))
    expect(new Set(leaves.flatMap(n => n.claimIds))).toEqual(new Set(['c1', 'c2', 'c3']))
    expect(graph.edges.every(e => graph.nodes.some(n => n.id === e.source) && graph.nodes.some(n => n.id === e.target))).toBe(true)
    expect(new Set(graph.edges.map(e => e.target)).size).toBe(graph.edges.length)
  })
})
