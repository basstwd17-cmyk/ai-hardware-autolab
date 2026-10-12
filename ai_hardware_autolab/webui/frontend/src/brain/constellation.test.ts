import {describe, expect, it} from 'vitest'
import {buildTopicGraph, TOPIC_ROOT} from './topics'
import {foldKnowledgeEvents, type KnowledgeEvent} from './model'
import {buildConstellation, fitStars, hitStar, zoomAt} from './constellation'

function fixture() {
  return buildTopicGraph(foldKnowledgeEvents(Array.from({length: 90}, (_, i): KnowledgeEvent => ({
    event_id: i + 1, at: 'now', kind: 'knowledge.proposed', claim_id: `c${i}`, version_id: `v${i}`,
    payload: {candidate: {subject: {kind: 'board', id: `board-${i % 2}`, label: `Board ${i % 2}`},
      scope: {hardware_id: `board-${i % 2}`, revision: i < 45 ? 'r1' : 'r2'},
      statement: ['PRU SPI GPIO timing', 'CPLD JTAG', 'RTM3004 waveform'][i % 3],
      relation: i < 5 ? 'requires_material' : 'supports', object: {kind: 'literal', value: i}, evidence: [], tags: []}},
  }))))
}

describe('whole knowledge constellation', () => {
  it('places each real record once and retains all alternate leaf memberships', () => {
    const graph = fixture(), map = buildConstellation(graph)
    const records = map.nodes.filter(n => n.claimId)
    expect(records).toHaveLength(90)
    expect(new Set(records.map(n => n.claimId)).size).toBe(90)
    expect(map.links.every(e => map.byId.has(e.source) && map.byId.has(e.target))).toBe(true)
    const parentIds = new Set(graph.edges.map(e => e.source))
    for (const leaf of graph.nodes.filter(n => !parentIds.has(n.id))) {
      expect(map.links.filter(e => e.source === leaf.id).map(e => map.byId.get(e.target)?.claimId).sort())
        .toEqual([...leaf.claimIds].sort())
    }
    for (const record of records) {
      expect(map.links.filter(e => e.target === record.id && !e.secondary)).toHaveLength(1)
      expect(graph.claims.find(c => c.claimId === record.claimId)?.status).toBe(record.status)
    }
    const history = map.nodes.find(n => n.label === '과거 절차 기록')!
    expect(records.filter(n => n.parentId === history.id)).toHaveLength(5)
  })

  it('keeps layout deterministic, finite, separated, and independent of source order', () => {
    const graph = fixture(), before = JSON.stringify(graph)
    const map = buildConstellation(graph)
    const second = buildConstellation({...graph, claims: [...graph.claims].reverse(), nodes: [...graph.nodes].reverse()})
    for (const n of map.nodes) {
      expect(Number.isFinite(n.x) && Number.isFinite(n.y)).toBe(true)
      expect(second.byId.get(n.id)?.x).toBeCloseTo(n.x, 6)
      expect(second.byId.get(n.id)?.y).toBeCloseTo(n.y, 6)
      const children = map.nodes.filter(child => child.navigation && child.parentId === n.id)
      for (let i = 0; i < children.length; i++) for (const other of children.slice(i + 1)) {
        expect(Math.hypot(children[i].x - other.x, children[i].y - other.y))
          .toBeGreaterThanOrEqual(children[i].clusterRadius + other.clusterRadius)
      }
    }
    expect(JSON.stringify(graph)).toBe(before)
    expect(map.nodes.filter(n => n.label.startsWith('Board '))).toHaveLength(4)
  })

  it('fits, zooms around the pointer and selects the closest actual point after panning', () => {
    const map = buildConstellation(fixture()), camera = fitStars(map.nodes, 1100, 700)
    for (const n of map.nodes) {
      expect(n.x * camera.scale + camera.x).toBeGreaterThan(0)
      expect(n.x * camera.scale + camera.x).toBeLessThan(1100)
      expect(n.y * camera.scale + camera.y).toBeGreaterThan(0)
      expect(n.y * camera.scale + camera.y).toBeLessThan(700)
    }
    const next = zoomAt(camera, 240, 160, 2)
    expect((240 - next.x) / next.scale).toBeCloseTo((240 - camera.x) / camera.scale)
    const record = map.nodes.find(n => n.claimId)!
    next.x += 50
    expect(hitStar(map.nodes, next, record.x * next.scale + next.x, record.y * next.scale + next.y)?.id).toBe(record.id)
    expect(hitStar(map.nodes, next, -100000, -100000)).toBeUndefined()
    const empty = buildConstellation(buildTopicGraph(foldKnowledgeEvents([])))
    expect(empty.nodes).toHaveLength(7)
    expect(empty.nodes.filter(n => n.claimId)).toHaveLength(0)
    expect(empty.byId.has(TOPIC_ROOT)).toBe(true)
  })
})
