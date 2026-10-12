/** A display projection of the topic tree. Positions never become knowledge. */
import type { ExplorerGraph, ExplorerNode } from './explorer'
import type { AtlasCategory } from './atlas'
import { TOPIC_ROOT } from './topics'

export interface Star extends ExplorerNode {
  x: number
  y: number
  radius: number
  depth: number
  parentId?: string
  clusterRadius: number
}
export interface StarLink {source: string; target: string; secondary?: boolean}
export interface Constellation {nodes: Star[]; links: StarLink[]; byId: Map<string, Star>}
export interface Camera {x: number; y: number; scale: number}

export const starColour = (category: AtlasCategory) => ({
  board: '#7da2d5', component: '#b39acd', circuit: '#70b3a5', software: '#8494c5',
  measurement: '#80bedb', document: '#c1aa70', datasheet: '#c1aa70', requirement: '#c798a9', other: '#ffe9ba',
})[category]

/** One point per stored record, with a primary placement and other topic links.
 * Topic membership is the existing navigation taxonomy, not semantic inference.
 */
export function buildConstellation(graph: ExplorerGraph): Constellation {
  const parents = new Map(graph.edges.map(e => [e.target, e.source]))
  const children = new Map<string, string[]>()
  for (const e of graph.edges) children.set(e.source, [...(children.get(e.source) ?? []), e.target])
  const depth = (id: string) => {
    let value = 0
    while (parents.has(id)) { id = parents.get(id)!; value++ }
    return value
  }
  const nodes: Star[] = graph.nodes.map(n => ({...n, x: 0, y: 0, radius: n.id === TOPIC_ROOT ? 7 : 2.8,
    depth: depth(n.id), parentId: parents.get(n.id), clusterRadius: 24}))
  const byId = new Map(nodes.map(n => [n.id, n]))
  const links: StarLink[] = graph.edges.map(e => ({source: e.source, target: e.target}))
  const leavesByClaim = new Map<string, Star[]>()
  for (const node of nodes.filter(n => !children.has(n.id))) {
    for (const id of node.claimIds) leavesByClaim.set(id, [...(leavesByClaim.get(id) ?? []), node])
  }
  const assigned = new Map<string, Star[]>()
  for (const claim of graph.claims) {
    const candidates = leavesByClaim.get(claim.claimId) ?? []
    // Specific engineering topics take display precedence over broad board /
    // source buckets. All alternate memberships remain available on selection.
    const rank = (n: Star) => (['board', 'document', 'datasheet'].includes(n.category) ? 0 : 10)
      + (n.label.toLowerCase() === claim.subject.label.toLowerCase() ? 20 : 0) + n.depth / 10
    const choices = [...candidates].sort((a, b) => rank(b) - rank(a) || a.id.localeCompare(b.id))
    const parent = choices[0] ?? byId.get(TOPIC_ROOT)
    if (!parent) continue
    const star: Star = {id: `star-record:${claim.claimId}`, kind: 'claim', label: claim.statement,
      claimId: claim.claimId, claimIds: [claim.claimId], status: claim.status, category: parent.category,
      searchText: claim.statement.toLocaleLowerCase(), degree: choices.length || 1,
      x: 0, y: 0, radius: 1.5, depth: parent.depth + 1, parentId: parent.id, clusterRadius: 0}
    nodes.push(star)
    byId.set(star.id, star)
    assigned.set(parent.id, [...(assigned.get(parent.id) ?? []), star])
    links.push({source: parent.id, target: star.id})
    for (const other of choices.slice(1)) links.push({source: other.id, target: star.id, secondary: true})
  }
  // Pack whole subtrees, keeping their bounding disks disjoint. Stable ordering
  // avoids an ongoing force simulation and preserves the camera on polling.
  const place = (id: string): {ids: string[]; radius: number} => {
    const n = byId.get(id)!
    const records = [...(assigned.get(id) ?? [])].sort((a, b) => a.id.localeCompare(b.id))
    const phase = hash(id) * Math.PI * 2
    records.forEach((record, i) => {
      const angle = phase + i * 2.399963229728653
      const r = 24 + 15 * Math.sqrt(i + 1)
      record.x = Math.cos(angle) * r
      record.y = Math.sin(angle) * r
    })
    const core = records.length ? 36 + 15 * Math.sqrt(records.length) : 14
    const parts = (children.get(id) ?? []).map(child => ({id: child, ...place(child)}))
      .sort((a, b) => b.radius - a.radius || a.id.localeCompare(b.id))
    const disks: Array<{x: number; y: number; r: number}> = [{x: 0, y: 0, r: core}]
    const ids = [id, ...records.map(record => record.id)]
    let radius = core
    for (const part of parts) {
      let x = 0, y = 0
      for (let attempt = 0; attempt <= 2400; attempt++) {
        const angle = phase + attempt * 2.399963229728653
        const r = core + part.radius + 20 + 7 * Math.sqrt(attempt) * (1 + attempt / 160)
        x = Math.cos(angle) * r; y = Math.sin(angle) * r
        if (disks.every(d => Math.hypot(x - d.x, y - d.y) >= d.r + part.radius + 16)) break
      }
      disks.push({x, y, r: part.radius})
      for (const child of part.ids) { byId.get(child)!.x += x; byId.get(child)!.y += y }
      ids.push(...part.ids)
      radius = Math.max(radius, Math.hypot(x, y) + part.radius)
    }
    n.clusterRadius = radius
    n.radius = id === TOPIC_ROOT ? 8 : n.depth === 1 ? 4.5 : 2.7
    return {ids, radius}
  }
  if (byId.has(TOPIC_ROOT)) place(TOPIC_ROOT)
  // Rotate the unchanged geometry to use a typical landscape map viewport.
  // Distances, cluster membership and disk separation are preserved.
  let bestAngle = 0, bestSize = Infinity
  for (let step = 0; step < 18; step++) {
    const angle = step * Math.PI / 18, cos = Math.cos(angle), sin = Math.sin(angle)
    const xs = nodes.map(n => n.x * cos - n.y * sin), ys = nodes.map(n => n.x * sin + n.y * cos)
    const size = Math.max((Math.max(...xs) - Math.min(...xs)) / 1.7, Math.max(...ys) - Math.min(...ys))
    if (size < bestSize) { bestSize = size; bestAngle = angle }
  }
  for (const n of nodes) {
    const {x, y} = n
    n.x = x * Math.cos(bestAngle) - y * Math.sin(bestAngle)
    n.y = x * Math.sin(bestAngle) + y * Math.cos(bestAngle)
  }
  return {nodes, links, byId}
}

function hash(text: string) {
  let n = 2166136261
  for (const c of text) n = Math.imul(n ^ c.charCodeAt(0), 16777619)
  return (n >>> 0) / 0xffffffff
}

export function fitStars(nodes: Star[], width: number, height: number): Camera {
  if (!nodes.length) return {x: width / 2, y: height / 2, scale: 1}
  const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y)
  const left = Math.min(...xs) - 75, right = Math.max(...xs) + 75
  const top = Math.min(...ys) - 75, bottom = Math.max(...ys) + 95
  const scale = Math.max(.015, Math.min(1.8, (width - 70) / (right - left), (height - 70) / (bottom - top)))
  return {x: width / 2 - (left + right) * scale / 2, y: height / 2 - (top + bottom) * scale / 2, scale}
}

export function zoomAt(camera: Camera, x: number, y: number, factor: number): Camera {
  const scale = Math.max(.015, Math.min(12, camera.scale * factor))
  const ratio = scale / camera.scale
  return {scale, x: x - (x - camera.x) * ratio, y: y - (y - camera.y) * ratio}
}

export function hitStar(nodes: Star[], camera: Camera, x: number, y: number): Star | undefined {
  let hit: Star | undefined, best = Infinity
  for (const node of nodes) {
    const distance = Math.hypot(node.x * camera.scale + camera.x - x, node.y * camera.scale + camera.y - y)
    const radius = Math.max(7, node.radius * camera.scale + 4)
    if (distance <= radius && distance < best) { best = distance; hit = node }
  }
  return hit
}
