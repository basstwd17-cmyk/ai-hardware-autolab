/**
 * Where nodes go when nobody has said where they should go.
 *
 * ELK's layered algorithm, because the graph is a provenance graph and reads
 * best as a flow: a job on the left, what it was run against to the right,
 * what it produced further right again. A force-directed layout would make the
 * same data look like a cloud with no direction in it.
 *
 * Manual placement wins. A node the user has dragged keeps its position and is
 * fed back to ELK as a fixed point, so the rest lays out *around* it rather
 * than reflowing out from under it. That is P2-D's persistence, and this is the
 * hook it plugs into.
 */

import ELK from 'elkjs/lib/elk.bundled.js'
import type { GraphEdge, GraphNode } from '../api/client'

const elk = new ELK()

export interface Placed {
  id: string
  x: number
  y: number
}

/** Node box size. Kept here so the layout and the renderer cannot disagree. */
export const NODE_WIDTH = 190
export const NODE_HEIGHT = 44

const OPTIONS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.layered.spacing.nodeNodeBetweenLayers': '90',
  'elk.spacing.nodeNode': '28',
  // Without this, a node with many edges (an `operation` group node, which is
  // the whole point of having group nodes) drags its neighbours into a line.
  'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
  'elk.layered.crossingMinimization.semiInteractive': 'true',
}

export async function layout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  pinned: Record<string, Placed> = {},
): Promise<Record<string, Placed>> {
  if (nodes.length === 0) return {}

  const known = new Set(nodes.map((n) => n.id))
  const graph = {
    id: 'root',
    layoutOptions: OPTIONS,
    children: nodes.map((node) => {
      const fixed = pinned[node.id]
      return {
        id: node.id,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        ...(fixed
          ? { x: fixed.x, y: fixed.y, layoutOptions: { 'elk.position': `(${fixed.x},${fixed.y})` } }
          : {}),
      }
    }),
    // An edge to a node the snapshot does not contain would make ELK throw and
    // take the whole view with it. The backend does not produce them, so this
    // is a guard rather than a workaround -- but a view that renders nothing
    // is a worse failure than one that omits an edge.
    edges: edges
      .filter((edge) => known.has(edge.source) && known.has(edge.target))
      .map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  }

  const done = await elk.layout(graph)
  const placed: Record<string, Placed> = {}
  for (const child of done.children ?? []) {
    placed[child.id as string] = {
      id: child.id as string,
      x: child.x ?? 0,
      y: child.y ?? 0,
    }
  }
  return placed
}
