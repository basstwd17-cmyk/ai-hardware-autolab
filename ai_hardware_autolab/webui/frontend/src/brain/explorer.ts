import { categoryForNode, type AtlasCategory } from './atlas'
import { buildTopicGraph } from './topics'
import {
  compactBrainGraph,
  objectLabel,
  type BrainClaim,
  type BrainEdge,
  type BrainGraph,
  type BrainNode,
} from './model'

export type ExplorerMode = 'topics' | 'concepts' | 'evidence'

export interface ExplorerNode extends BrainNode {
  navigation?: boolean
  category: AtlasCategory
  claimIds: string[]
  degree: number
  searchText: string
}

export interface ExplorerGraph {
  claims: BrainClaim[]
  nodes: ExplorerNode[]
  edges: BrainEdge[]
  cursor: number
}

export interface ExplorerConnection {
  direction: 'in' | 'out'
  edge: BrainEdge
  peer: ExplorerNode
}

const STATUS_PRIORITY = ['refuted', 'verified', 'proposed', 'superseded']

/**
 * Turn the durable projection into an Obsidian-shaped network. Concepts is the
 * quiet default; Evidence exposes the claim, source and job nodes without
 * changing any underlying fact.
 */
export function buildExplorerGraph(source: BrainGraph, mode: ExplorerMode): ExplorerGraph {
  if (mode === 'topics') return buildTopicGraph(source)
  const base = mode === 'concepts' ? compactBrainGraph(source) : source
  const claims = new Map(source.claims.map((claim) => [claim.claimId, claim]))
  const claimsByNode = new Map<string, Set<string>>()
  const degree = new Map<string, number>()

  for (const node of base.nodes) {
    if (node.claimId) add(claimsByNode, node.id, node.claimId)
    if (node.id.startsWith('claim:')) add(claimsByNode, node.id, node.id.slice(6))
  }
  for (const edge of base.edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1)
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1)
    if (edge.claimId) {
      add(claimsByNode, edge.source, edge.claimId)
      add(claimsByNode, edge.target, edge.claimId)
    }
  }

  const nodes = base.nodes.map((node): ExplorerNode => {
    const claimIds = [...(claimsByNode.get(node.id) ?? [])].sort()
    const related = claimIds.map((id) => claims.get(id)).filter((claim): claim is BrainClaim => Boolean(claim))
    const primary = related[0]
    const category = categoryForExplorerNode(node, primary)
    const statuses = related.map((claim) => claim.status)
    const status = STATUS_PRIORITY.find((candidate) => statuses.includes(candidate)) ?? node.status
    return {
      ...node,
      category,
      claimIds,
      degree: degree.get(node.id) ?? 0,
      status,
      searchText: [
        node.label,
        node.kind,
        category,
        ...related.flatMap((claim) => [
          claim.statement,
          claim.relation,
          claim.subject.label,
          objectLabel(claim.object),
          claim.sourceJobId,
          ...claim.tags,
        ]),
      ].join(' ').toLocaleLowerCase(),
    }
  })
  return { claims: source.claims, nodes, edges: base.edges, cursor: source.cursor }
}

export function filterExplorerGraph(
  graph: ExplorerGraph,
  categories: ReadonlySet<AtlasCategory>,
  statuses: ReadonlySet<string>,
): ExplorerGraph {
  const kept = new Set(
    graph.nodes
      .filter((node) => categories.has(node.category) && (!node.status || statuses.has(node.status)))
      .map((node) => node.id),
  )
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => kept.has(node.id)),
    edges: graph.edges.filter((edge) => kept.has(edge.source) && kept.has(edge.target)),
  }
}

/** Undirected breadth-first neighborhood, matching Obsidian's Local Graph. */
export function localExplorerGraph(graph: ExplorerGraph, focus: string, depth: number): ExplorerGraph {
  if (!graph.nodes.some((node) => node.id === focus)) return graph
  const boundedDepth = Math.max(1, Math.min(3, Math.floor(depth)))
  const adjacency = new Map<string, Set<string>>()
  for (const edge of graph.edges) {
    add(adjacency, edge.source, edge.target)
    add(adjacency, edge.target, edge.source)
  }
  const kept = new Set([focus])
  let frontier = new Set([focus])
  for (let level = 0; level < boundedDepth; level += 1) {
    const next = new Set<string>()
    for (const node of frontier) {
      for (const peer of adjacency.get(node) ?? []) {
        if (!kept.has(peer)) next.add(peer)
        kept.add(peer)
      }
    }
    frontier = next
  }
  return {
    ...graph,
    nodes: graph.nodes.filter((node) => kept.has(node.id)),
    edges: graph.edges.filter((edge) => kept.has(edge.source) && kept.has(edge.target)),
  }
}

export function matchingExplorerNodeIds(graph: ExplorerGraph, query: string): Set<string> {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return new Set()
  return new Set(graph.nodes.filter((node) => node.searchText.includes(needle)).map((node) => node.id))
}

export function connectionsForNode(graph: ExplorerGraph, nodeId: string): ExplorerConnection[] {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]))
  const result: ExplorerConnection[] = []
  for (const edge of graph.edges) {
    if (edge.source === nodeId && nodes.has(edge.target)) {
      result.push({ direction: 'out', edge, peer: nodes.get(edge.target)! })
    } else if (edge.target === nodeId && nodes.has(edge.source)) {
      result.push({ direction: 'in', edge, peer: nodes.get(edge.source)! })
    }
  }
  return result.sort((left, right) => (
    left.peer.label.localeCompare(right.peer.label) || left.edge.label.localeCompare(right.edge.label)
  ))
}

export function claimsForNode(graph: ExplorerGraph, nodeId: string): BrainClaim[] {
  const ids = new Set(graph.nodes.find((node) => node.id === nodeId)?.claimIds ?? [])
  return graph.claims.filter((claim) => ids.has(claim.claimId))
}

/**
 * Small deterministic force simulation. It gives the same durable graph the
 * same starting constellation, while React Flow still lets a person drag and
 * keep a preferred arrangement in this browser.
 */
export function forceExplorerLayout(graph: ExplorerGraph): Map<string, { x: number; y: number }> {
  const ordered = [...graph.nodes].sort((left, right) => left.id.localeCompare(right.id))
  const positions = new Map<string, { x: number; y: number }>()
  const velocity = new Map<string, { x: number; y: number }>()
  ordered.forEach((node, index) => {
    const angle = (stableNumber(node.id) / 0xffffffff) * Math.PI * 2
    const radius = 110 + 20 * Math.sqrt(index + 1)
    positions.set(node.id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius })
    velocity.set(node.id, { x: 0, y: 0 })
  })
  if (ordered.length < 2) return positions

  const links = graph.edges.filter((edge) => positions.has(edge.source) && positions.has(edge.target))
  for (let iteration = 0; iteration < 150; iteration += 1) {
    const force = new Map(ordered.map((node) => [node.id, { x: 0, y: 0 }]))
    for (let i = 0; i < ordered.length; i += 1) {
      for (let j = i + 1; j < ordered.length; j += 1) {
        const left = positions.get(ordered[i].id)!
        const right = positions.get(ordered[j].id)!
        let dx = right.x - left.x
        let dy = right.y - left.y
        if (dx === 0 && dy === 0) dx = 0.01
        const distanceSquared = Math.max(100, dx * dx + dy * dy)
        const distance = Math.sqrt(distanceSquared)
        const strength = Math.min(7, 8_000 / distanceSquared)
        dx = (dx / distance) * strength
        dy = (dy / distance) * strength
        force.get(ordered[i].id)!.x -= dx
        force.get(ordered[i].id)!.y -= dy
        force.get(ordered[j].id)!.x += dx
        force.get(ordered[j].id)!.y += dy
      }
    }
    for (const edge of links) {
      const source = positions.get(edge.source)!
      const target = positions.get(edge.target)!
      const dx = target.x - source.x
      const dy = target.y - source.y
      const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy))
      const strength = (distance - 145) * 0.012
      const fx = (dx / distance) * strength
      const fy = (dy / distance) * strength
      force.get(edge.source)!.x += fx
      force.get(edge.source)!.y += fy
      force.get(edge.target)!.x -= fx
      force.get(edge.target)!.y -= fy
    }
    for (const node of ordered) {
      const point = positions.get(node.id)!
      const push = force.get(node.id)!
      const speed = velocity.get(node.id)!
      push.x -= point.x * 0.003
      push.y -= point.y * 0.003
      speed.x = (speed.x + push.x) * 0.82
      speed.y = (speed.y + push.y) * 0.82
      point.x += speed.x
      point.y += speed.y
    }
  }
  return positions
}

function categoryForExplorerNode(node: BrainNode, claim?: BrainClaim): AtlasCategory {
  if (node.kind === 'evidence') {
    return claim?.tags.some((tag) => tag.toLocaleLowerCase() === 'datasheet') ? 'datasheet' : 'document'
  }
  if (node.kind === 'job') return 'software'
  if (node.kind === 'claim' && claim) {
    return categoryForNode(
      { id: node.id, kind: 'entity', label: claim.subject.label, claimId: claim.claimId },
      claim,
    )
  }
  return categoryForNode(node, claim)
}

function add(map: Map<string, Set<string>>, key: string, value: string): void {
  const values = map.get(key) ?? new Set<string>()
  values.add(value)
  map.set(key, values)
}

function stableNumber(value: string): number {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}
