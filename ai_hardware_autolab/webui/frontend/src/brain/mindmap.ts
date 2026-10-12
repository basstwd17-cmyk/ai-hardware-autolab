import type { ExplorerGraph, ExplorerNode } from './explorer'
import { matchesBrainQuery, type BrainClaim } from './model'
import { TOPIC_ROOT } from './topics'

export const BRANCH_PAGE_SIZE = 10

export function topicPath(graph: ExplorerGraph, id: string): ExplorerNode[] {
  const nodes = new Map(graph.nodes.map(n => [n.id, n]))
  const parents = new Map(graph.edges.map(e => [e.target, e.source]))
  const path: ExplorerNode[] = []
  const seen = new Set<string>()
  let current: string | undefined = id
  while (current && nodes.has(current) && !seen.has(current)) {
    seen.add(current)
    path.unshift(nodes.get(current)!)
    current = parents.get(current)
  }
  return path
}

export function topicForClaim(graph: ExplorerGraph, claimId: string): ExplorerNode | undefined {
  // Prefer the specific board/revision branch, then the deepest classification.
  return graph.nodes.filter(n => n.claimIds.includes(claimId)).sort((a, b) =>
    Number(b.id.startsWith('topic:board:')) - Number(a.id.startsWith('topic:board:'))
    || topicPath(graph, b.id).length - topicPath(graph, a.id).length
    || a.id.localeCompare(b.id),
  )[0]
}

export function searchTopicClaims(graph: ExplorerGraph, query: string): BrainClaim[] {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return []
  return graph.claims.filter(c => matchesBrainQuery(c, query)
    || Object.values(c.scope).some(value => value.toLocaleLowerCase().includes(needle)))
}

/** Show a single branch, paging wide branches as well as their record leaves. */
export function topicBranch(graph: ExplorerGraph, focusId: string, requestedPage = 0) {
  const focus = graph.nodes.find(n => n.id === focusId) ?? graph.nodes.find(n => n.id === TOPIC_ROOT)!
  const ids = new Set(graph.edges.filter(e => e.source === focus.id).map(e => e.target))
  const children = graph.nodes.filter(n => ids.has(n.id))
  const claims = new Set(focus.claimIds)
  const rows: ExplorerNode[] = children.length ? children : graph.claims.filter(c => claims.has(c.claimId)).map(c => ({
    id: `topic-record:${c.claimId}`, kind: 'claim', label: c.statement, status: c.status,
    category: focus.category, claimId: c.claimId, claimIds: [c.claimId], degree: 1, searchText: c.statement.toLocaleLowerCase(),
  }))
  const pages = Math.max(1, Math.ceil(rows.length / BRANCH_PAGE_SIZE))
  const page = Math.max(0, Math.min(pages - 1, requestedPage))
  const shown = rows.slice(page * BRANCH_PAGE_SIZE, (page + 1) * BRANCH_PAGE_SIZE)
  const edges = shown.map(n => ({id: `topic-link:${focus.id}:${n.id}`, source: focus.id, target: n.id,
    label: n.navigation ? '하위 분류' : '기록', claimId: n.claimId, status: n.status}))
  return { graph: {...graph, nodes: [focus, ...shown], edges}, focus, page, pages, total: rows.length }
}

export function mindMapLayout(graph: ExplorerGraph): Map<string, { x: number; y: number }> {
  const count = graph.nodes.length - 1
  return new Map(graph.nodes.map((node, index) => {
    if (index === 0) return [node.id, {x: 0, y: 0}]
    const side = (index - 1) % 2
    const sideCount = side === 0 ? Math.ceil(count / 2) : Math.floor(count / 2)
    return [node.id, {x: side === 0 ? 330 : -330, y: (Math.floor((index - 1) / 2) - (sideCount - 1) / 2) * 108}]
  }))
}
