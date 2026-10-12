/**
 * A compact, category-first view of the durable knowledge graph.
 *
 * The atlas is presentation only: it never creates a claim or an edge between
 * two facts.  It adds one root per deterministic category and hangs each
 * existing concept or source underneath it.  The original claim graph remains
 * available for provenance inspection.
 */

import { compactBrainGraph, conceptLabel, type BrainClaim, type BrainEdge, type BrainGraph, type BrainNode } from './model'

export type AtlasCategory =
  | 'board'
  | 'component'
  | 'circuit'
  | 'datasheet'
  | 'document'
  | 'software'
  | 'measurement'
  | 'requirement'
  | 'other'

export interface AtlasNode extends BrainNode {
  category: AtlasCategory
  root?: boolean
  count?: number
}

export interface AtlasGraph {
  nodes: AtlasNode[]
  edges: BrainEdge[]
}

export const ATLAS_CATEGORIES: Array<{ id: AtlasCategory; label: string }> = [
  { id: 'board', label: 'Board' },
  { id: 'component', label: 'Component' },
  { id: 'circuit', label: 'Circuit' },
  { id: 'datasheet', label: 'Datasheet' },
  { id: 'document', label: 'Docs' },
  { id: 'software', label: 'Software' },
  { id: 'measurement', label: 'Measurement' },
  { id: 'requirement', label: 'Requirement' },
  { id: 'other', label: 'Other' },
]

const BOARD = /\b(bbb|beaglebone|stm32|nucleo|teensy|raspberry\s*pi|pru)\b/i
const COMPONENT = /\b(cpld|fpga|dac|adc|oscillator|crystal|clock.generator|regulator|lattice|eclipse|power.supply)\b/i
const CIRCUIT = /\b(spi|gpio|rf[12]?|clock|timing|signal|pin.map|jtag|i2c|uart|power|voltage)\b/i
const SOFTWARE = /\b(verilog|vhdl|python|firmware|overlay|driver|build|diamond|pru|host)\b/i
const MEASUREMENT = /\b(scope|oscilloscope|measurement|frequency|latency|jitter|timing)\b/i

/** Build an honest taxonomy from existing labels, kinds, tags and evidence. */
export function buildAtlasGraph(graph: BrainGraph): AtlasGraph {
  const compact = compactBrainGraph(graph)
  const claimById = new Map(graph.claims.map((claim) => [claim.claimId, claim]))
  const categoryByNode = new Map<string, AtlasCategory>()
  const nodes = new Map<string, AtlasNode>()

  for (const node of compact.nodes) {
    const claim = node.claimId ? claimById.get(node.claimId) : undefined
    const category = categoryForNode(node, claim)
    categoryByNode.set(node.id, category)
    nodes.set(node.id, {
      ...node,
      category,
      label: conceptLabel(node.label, 3, 28),
    })
  }

  // Source documents are knowledge too.  They become small points below Docs
  // or Datasheet, while their evidence remains visible in the claim inspector.
  for (const claim of graph.claims) {
    for (const evidence of claim.evidence) {
      const id = `atlas:evidence:${evidence.kind}:${evidence.sha256}:${evidence.page ?? ''}`
      const category: AtlasCategory = hasTag(claim, 'datasheet') ? 'datasheet' : 'document'
      categoryByNode.set(id, category)
      if (!nodes.has(id)) {
        nodes.set(id, {
          id,
          kind: 'evidence',
          claimId: claim.claimId,
          category,
          label: evidence.page ? `source p.${evidence.page}` : 'source',
        })
      }
    }
  }

  const counts = new Map<AtlasCategory, number>()
  for (const category of categoryByNode.values()) counts.set(category, (counts.get(category) ?? 0) + 1)
  for (const { id, label } of ATLAS_CATEGORIES) {
    nodes.set(`atlas:${id}`, {
      id: `atlas:${id}`,
      kind: 'entity',
      category: id,
      root: true,
      count: counts.get(id) ?? 0,
      label,
    })
  }

  const edges: BrainEdge[] = []
  for (const node of nodes.values()) {
    if (node.root) continue
    edges.push({
      id: `atlas:contains:${node.category}:${node.id}`,
      source: `atlas:${node.category}`,
      target: node.id,
      label: '',
      claimId: node.claimId,
    })
  }
  for (const edge of compact.edges) {
    // Keep only direct, evidence-backed concept relations.  Category lines are
    // intentionally visually quieter than these edges.
    edges.push({ ...edge, id: `atlas:fact:${edge.id}` })
  }
  for (const claim of graph.claims) {
    for (const evidence of claim.evidence) {
      const source = `atlas:evidence:${evidence.kind}:${evidence.sha256}:${evidence.page ?? ''}`
      const target = entityIdForClaim(claim)
      if (nodes.has(target)) {
        edges.push({ id: `atlas:supports:${source}:${claim.claimId}`, source, target, label: '', claimId: claim.claimId, status: claim.status })
      }
    }
  }
  return { nodes: [...nodes.values()], edges }
}

export function categoryForNode(node: BrainNode, claim?: BrainClaim): AtlasCategory {
  const text = `${node.label} ${node.kind} ${claim?.relation ?? ''} ${(claim?.tags ?? []).join(' ')}`
  if (hasTag(claim, 'datasheet')) return 'datasheet'
  if (BOARD.test(text)) return 'board'
  if (COMPONENT.test(text)) return 'component'
  if (CIRCUIT.test(text)) return 'circuit'
  if (node.kind === 'evidence' || hasTag(claim, 'research')) return 'document'
  if (MEASUREMENT.test(text)) return 'measurement'
  if (node.kind === 'literal' || /question|requirement|decision/.test(text)) return 'requirement'
  if (node.kind === 'entity' && SOFTWARE.test(text)) return 'software'
  return 'other'
}

function hasTag(claim: BrainClaim | undefined, tag: string): boolean {
  return claim?.tags.some((value) => value.toLowerCase() === tag) ?? false
}

function entityIdForClaim(claim: BrainClaim): string {
  return `entity:${claim.subject.kind}:${claim.subject.id}`
}
