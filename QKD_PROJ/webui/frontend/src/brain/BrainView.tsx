import { Background, Controls, MiniMap, ReactFlow, useNodesState, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Api } from '../api/client'
import { ATLAS_CATEGORIES, type AtlasCategory } from './atlas'
import {
  buildExplorerGraph,
  claimsForNode,
  connectionsForNode,
  filterExplorerGraph,
  forceExplorerLayout,
  localExplorerGraph,
  matchingExplorerNodeIds,
  type ExplorerGraph,
  type ExplorerMode,
  type ExplorerNode,
} from './explorer'
import {
  conceptLabel,
  foldKnowledgeEvents,
  objectLabel,
  type BrainClaim,
  type BrainEdge,
  type KnowledgeEvent,
} from './model'

const STATUS_COLOUR: Record<string, string> = {
  verified: '#55d69e', refuted: '#ef7080', proposed: '#e5b454', superseded: '#69727f',
}
const STATUSES = ['verified', 'refuted', 'proposed', 'superseded']
const POSITION_KEY = 'rtase.brain.positions.v2'

export function BrainView({ api }: { api: Api | null }) {
  const [events, setEvents] = useState<KnowledgeEvent[]>([])
  const [latest, setLatest] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [statuses, setStatuses] = useState<Set<string>>(new Set(['verified', 'refuted', 'proposed']))
  const [categories, setCategories] = useState<Set<AtlasCategory>>(
    new Set(ATLAS_CATEGORIES.map(({ id }) => id)),
  )
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedClaimId, setSelectedClaimId] = useState<string | null>(null)
  const [mode, setMode] = useState<ExplorerMode>('concepts')
  const [scope, setScope] = useState<'global' | 'local'>('global')
  const [depth, setDepth] = useState(1)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const savedPositions = useRef(loadPositions())

  const load = useCallback(async (background = false) => {
    if (!api) return
    if (!background) setLoading(true)
    setError('')
    try {
      let after = 0
      let tip = 0
      const rows: KnowledgeEvent[] = []
      for (let page = 0; page < 20; page += 1) {
        const answer = await api.command({
          type: 'knowledge.events', commandId: crypto.randomUUID(), after, limit: 500,
        })
        if (answer.ok !== true) throw new Error(String(answer.error ?? 'knowledge read was refused'))
        const knowledge = record(answer.knowledge)
        const batch = Array.isArray(knowledge.events) ? (knowledge.events as KnowledgeEvent[]) : []
        rows.push(...batch)
        const cursor = number(knowledge.cursor, after)
        tip = number(knowledge.latest, cursor)
        if (cursor >= tip || batch.length === 0) break
        after = cursor
      }
      setEvents(rows)
      setLatest(tip)
      if ((rows.at(-1)?.event_id ?? 0) < tip) {
        setError(`Loaded ${rows.length} rows but the knowledge ledger continues to ${tip}.`)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'knowledge read failed')
    } finally {
      if (!background) setLoading(false)
    }
  }, [api])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => { void load(true) }, 5_000)
    return () => window.clearInterval(timer)
  }, [load])

  const source = useMemo(() => foldKnowledgeEvents(events), [events])
  const complete = useMemo(() => buildExplorerGraph(source, mode), [source, mode])
  const filtered = useMemo(
    () => filterExplorerGraph(complete, categories, statuses),
    [complete, categories, statuses],
  )
  const shown = useMemo(
    () => scope === 'local' && selectedNodeId
      ? localExplorerGraph(filtered, selectedNodeId, depth)
      : filtered,
    [filtered, scope, selectedNodeId, depth],
  )
  const matches = useMemo(() => matchingExplorerNodeIds(filtered, query), [filtered, query])
  const layout = useMemo(() => forceExplorerLayout(shown), [shown])
  const selectedNode = complete.nodes.find((node) => node.id === selectedNodeId) ?? null
  const selectedClaims = useMemo(
    () => selectedNodeId ? claimsForNode(complete, selectedNodeId) : [],
    [complete, selectedNodeId],
  )
  const selectedClaim = source.claims.find((claim) => claim.claimId === selectedClaimId)
    ?? selectedClaims[0]
    ?? null
  const connections = useMemo(
    () => selectedNodeId ? connectionsForNode(complete, selectedNodeId) : [],
    [complete, selectedNodeId],
  )
  const initialNodes = useMemo(() => shown.nodes.map((node) => explorerFlowNode(
    node,
    savedPositions.current[node.id] ?? layout.get(node.id) ?? { x: 0, y: 0 },
    selectedNodeId,
    matches,
    query,
  )), [shown.nodes, layout, selectedNodeId, matches, query])
  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node>(initialNodes)

  useEffect(() => {
    setFlowNodes((current) => {
      const currentPositions = new Map(current.map((node) => [node.id, node.position]))
      return initialNodes.map((node) => ({
        ...node,
        position: currentPositions.get(node.id) ?? node.position,
      }))
    })
  }, [initialNodes, setFlowNodes])

  useEffect(() => {
    if (selectedNodeId && !complete.nodes.some((node) => node.id === selectedNodeId)) {
      setSelectedNodeId(null)
      setSelectedClaimId(null)
      setScope('global')
    }
  }, [complete.nodes, selectedNodeId])

  const flowEdges: Edge[] = useMemo(() => shown.edges.map((edge) => explorerFlowEdge(
    edge,
    selectedNodeId,
  )), [shown.edges, selectedNodeId])

  const selectNode = useCallback((node: ExplorerNode) => {
    setSelectedNodeId(node.id)
    setSelectedClaimId(node.claimIds[0] ?? null)
    setInspectorOpen(true)
  }, [])

  const resetLayout = useCallback(() => {
    savedPositions.current = {}
    window.localStorage.removeItem(POSITION_KEY)
    setFlowNodes(shown.nodes.map((node) => explorerFlowNode(
      node,
      layout.get(node.id) ?? { x: 0, y: 0 },
      selectedNodeId,
      matches,
      query,
    )))
  }, [layout, matches, query, selectedNodeId, setFlowNodes, shown.nodes])

  return (
    <div className="brain obsidian-brain">
      <div className="brain-toolbar obsidian-toolbar">
        <label className="brain-search">
          <span>⌕</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search notes, boards, runs, evidence…"
          />
          {query && <b>{matches.size}</b>}
        </label>
        <div className="brain-toolbar-group">
          <button className={`chip ${mode === 'concepts' ? 'on' : 'off'}`} onClick={() => setMode('concepts')}>Concepts</button>
          <button className={`chip ${mode === 'evidence' ? 'on' : 'off'}`} onClick={() => setMode('evidence')}>Evidence</button>
        </div>
        <div className="brain-toolbar-group">
          <button className={`chip ${scope === 'global' ? 'on' : 'off'}`} onClick={() => setScope('global')}>Global</button>
          <button
            className={`chip ${scope === 'local' ? 'on' : 'off'}`}
            disabled={!selectedNodeId}
            onClick={() => setScope('local')}
          >Local</button>
          {scope === 'local' && [1, 2, 3].map((value) => (
            <button key={value} className={`chip depth ${depth === value ? 'on' : 'off'}`} onClick={() => setDepth(value)}>{value}</button>
          ))}
        </div>
        <button className="link" onClick={resetLayout}>reflow</button>
        <button className="link" disabled={loading || !api} onClick={() => void load()}>{loading ? 'reading…' : 'refresh'}</button>
      </div>

      <div className="brain-filterbar">
        <span className="filter-title">Types</span>
        {ATLAS_CATEGORIES.map(({ id, label }) => {
          const count = complete.nodes.filter((node) => node.category === id).length
          if (count === 0) return null
          return (
            <button key={id} className={`chip category ${categories.has(id) ? 'on' : 'off'}`} onClick={() => setCategories(toggle(categories, id))}>
              <i style={{ background: categoryColour(id) }} />{label}<b>{count}</b>
            </button>
          )
        })}
        <span className="filter-title status-title">Truth</span>
        {STATUSES.map((status) => {
          const count = source.claims.filter((claim) => claim.status === status).length
          return (
            <button key={status} className={`chip ${statuses.has(status) ? 'on' : 'off'}`} onClick={() => setStatuses(toggle(statuses, status))}>
              <i style={{ background: STATUS_COLOUR[status] }} />{status}<b>{count}</b>
            </button>
          )
        })}
      </div>

      <div className={`brain-body obsidian-body ${inspectorOpen ? 'inspector-open' : ''}`}>
        <div className="brain-canvas obsidian-canvas">
          {events.length === 0 ? (
            <div className="empty">
              <h3>{error ? 'Knowledge is unavailable' : 'The durable brain is empty'}</h3>
              <p>{error || 'Research, plans, runs and verified measurements will accumulate here across jobs.'}</p>
            </div>
          ) : shown.nodes.length === 0 ? (
            <div className="empty"><h3>No notes match these filters</h3><p>Turn a type or truth filter back on.</p></div>
          ) : (
            <ReactFlow
              nodes={flowNodes}
              edges={flowEdges}
              onNodesChange={onNodesChange}
              fitView
              minZoom={0.08}
              maxZoom={2.4}
              onNodeClick={(_, node) => {
                const found = complete.nodes.find((candidate) => candidate.id === node.id)
                if (found) selectNode(found)
              }}
              onNodeDoubleClick={(_, node) => {
                const found = complete.nodes.find((candidate) => candidate.id === node.id)
                if (found) selectNode(found)
                setScope('local')
              }}
              onNodeDragStop={(_, node) => {
                savedPositions.current[node.id] = node.position
                storePositions(savedPositions.current)
              }}
              onEdgeClick={(_, edge) => {
                const claimId = edge.data?.claimId
                if (typeof claimId === 'string') setSelectedClaimId(claimId)
              }}
              onPaneClick={() => {
                if (scope === 'global') {
                  setSelectedNodeId(null)
                  setSelectedClaimId(null)
                }
              }}
              proOptions={{ hideAttribution: false }}
            >
              <Background color="#263140" gap={30} size={1} />
              <Controls showInteractive={false} />
              <MiniMap
                pannable
                zoomable
                nodeColor={(node) => String(node.data.colour ?? '#8290a3')}
                nodeStrokeWidth={0}
                style={{ background: '#0c1016' }}
              />
            </ReactFlow>
          )}
          <div className="brain-canvas-caption">
            {scope === 'local' && selectedNode
              ? `Local graph · ${selectedNode.label} · depth ${depth}`
              : 'Global graph'}
            <span>{shown.nodes.length} notes · {shown.edges.length} links</span>
          </div>
          <button className="brain-note-toggle" onClick={() => setInspectorOpen(true)}>note panel</button>
        </div>

        <aside className="brain-side obsidian-side">
          <button className="brain-note-close" onClick={() => setInspectorOpen(false)}>× close</button>
          {error && events.length > 0 && <p className="warn">{error}</p>}
          <p className="muted small brain-cursor">knowledge cursor {source.cursor}/{latest}</p>
          {selectedNode ? (
            <NoteInspector
              node={selectedNode}
              claims={selectedClaims}
              selectedClaim={selectedClaim}
              connections={connections}
              onSelectNode={(id) => {
                const found = complete.nodes.find((node) => node.id === id)
                if (found) selectNode(found)
              }}
              onSelectClaim={setSelectedClaimId}
            />
          ) : (
            <BrainOverview graph={complete} matches={matches} query={query} onSelect={selectNode} />
          )}
        </aside>
      </div>
    </div>
  )
}

function BrainOverview({
  graph, matches, query, onSelect,
}: {
  graph: ExplorerGraph
  matches: Set<string>
  query: string
  onSelect: (node: ExplorerNode) => void
}) {
  const results = graph.nodes.filter((node) => matches.has(node.id)).slice(0, 12)
  const hubs = [...graph.nodes].sort((left, right) => right.degree - left.degree).slice(0, 8)
  return (
    <section className="brain-note overview-note">
      <header><small>Durable brain</small><h2>{query ? 'Search results' : 'Knowledge graph'}</h2></header>
      <p className="note-lead">
        {query
          ? `${matches.size} notes mention “${query}”. Select one to open its local graph and backlinks.`
          : 'Select a point to read the fact, its evidence, connected notes and development history.'}
      </p>
      <h3>{query ? 'Matches' : 'Connected notes'}</h3>
      <div className="note-link-list">
        {(query ? results : hubs).map((node) => (
          <button key={node.id} onClick={() => onSelect(node)}>
            <i style={{ background: categoryColour(node.category) }} />
            <span>{node.label}<small>{node.category} · {node.degree} links</small></span>
          </button>
        ))}
      </div>
      <div className="brain-overview-stats">
        <Metric label="notes" value={graph.nodes.length} />
        <Metric label="links" value={graph.edges.length} />
        <Metric label="claims" value={graph.claims.length} />
      </div>
    </section>
  )
}

function NoteInspector({
  node, claims, selectedClaim, connections, onSelectNode, onSelectClaim,
}: {
  node: ExplorerNode
  claims: BrainClaim[]
  selectedClaim: BrainClaim | null
  connections: ReturnType<typeof connectionsForNode>
  onSelectNode: (id: string) => void
  onSelectClaim: (id: string) => void
}) {
  const incoming = connections.filter((connection) => connection.direction === 'in')
  const outgoing = connections.filter((connection) => connection.direction === 'out')
  const evidence = uniqueEvidence(claims)
  const jobs = [...new Set(claims.map((claim) => claim.sourceJobId).filter(Boolean))]
  return (
    <section className="brain-note">
      <header>
        <div className="note-kicker"><i style={{ background: categoryColour(node.category) }} />{node.category} · {node.kind}</div>
        <h2>{node.label}</h2>
        <div className="note-badges">
          {node.status && <span style={{ borderColor: STATUS_COLOUR[node.status], color: STATUS_COLOUR[node.status] }}>{node.status}</span>}
          <span>{node.degree} links</span>
          <span>{claims.length} claims</span>
        </div>
      </header>

      {selectedClaim && (
        <div className="note-summary">
          <small>{selectedClaim.relation.replace(/[_-]+/g, ' ')}</small>
          <p>{selectedClaim.statement}</p>
          <dl className="note-fact">
            <Row label="subject" value={selectedClaim.subject.label} />
            <Row label="value" value={objectLabel(selectedClaim.object)} />
            <Row label="producer" value={selectedClaim.producer} />
            <Row label="updated" value={selectedClaim.at} />
          </dl>
        </div>
      )}

      <ConnectionList title="Links" rows={outgoing} onSelect={onSelectNode} />
      <ConnectionList title="Backlinks" rows={incoming} onSelect={onSelectNode} />

      {claims.length > 1 && (
        <>
          <h3>Claims</h3>
          <div className="claim-list">
            {claims.map((claim) => (
              <button key={claim.claimId} className={selectedClaim?.claimId === claim.claimId ? 'selected' : ''} onClick={() => onSelectClaim(claim.claimId)}>
                <i style={{ background: STATUS_COLOUR[claim.status] }} />
                <span>{conceptLabel(claim.statement, 8, 90)}<small>{claim.relation.replace(/[_-]+/g, ' ')}</small></span>
              </button>
            ))}
          </div>
        </>
      )}

      <h3>Sources <b>{evidence.length}</b></h3>
      {evidence.length === 0 ? <p className="muted small">No attached evidence. This remains a proposal or workflow note.</p> : (
        <div className="source-list">{evidence.map((item) => (
          <article key={item.key}>
            <strong>{item.role} · {item.kind}{item.page ? ` · p.${item.page}` : ''}</strong>
            <span>{item.locator}</span>
            <code>{item.sha256.slice(0, 16)}…</code>
            {item.excerpt && <q>{item.excerpt}</q>}
          </article>
        ))}</div>
      )}

      {jobs.length > 0 && <><h3>Development thread</h3><div className="job-links">{jobs.map((job) => <span key={job}>↳ {job}</span>)}</div></>}
      {selectedClaim && <details className="note-details"><summary>Identity and scope</summary><code>{selectedClaim.claimId}</code><pre>{JSON.stringify(selectedClaim.scope, null, 2)}</pre></details>}
    </section>
  )
}

function ConnectionList({ title, rows, onSelect }: {
  title: string
  rows: ReturnType<typeof connectionsForNode>
  onSelect: (id: string) => void
}) {
  if (rows.length === 0) return null
  return (
    <><h3>{title} <b>{rows.length}</b></h3><div className="note-link-list">
      {rows.map(({ edge, peer }) => (
        <button key={`${edge.id}:${peer.id}`} onClick={() => onSelect(peer.id)}>
          <i style={{ background: categoryColour(peer.category) }} />
          <span>{peer.label}<small>{edge.label.replace(/[_-]+/g, ' ')}</small></span>
        </button>
      ))}
    </div></>
  )
}

function explorerFlowNode(
  node: ExplorerNode,
  position: { x: number; y: number },
  selected: string | null,
  matches: Set<string>,
  query: string,
): Node {
  const colour = categoryColour(node.category)
  const size = Math.min(28, 12 + Math.sqrt(Math.max(node.degree, 1)) * 4)
  const searchDimmed = query.trim().length > 0 && !matches.has(node.id)
  const active = selected === node.id
  return {
    id: node.id,
    position,
    data: {
      colour,
      claimIds: node.claimIds,
      label: (
        <div className={`obsidian-node ${active ? 'selected' : ''} ${searchDimmed ? 'dimmed' : ''}`} title={node.label}>
          <i
            className="obsidian-orb"
            style={{
              width: size,
              height: size,
              background: colour,
              borderColor: STATUS_COLOUR[node.status ?? 'proposed'],
            }}
          />
          <span>{conceptLabel(node.label, 4, 38)}</span>
        </div>
      ),
    },
    style: {
      width: 128,
      height: 54,
      border: 'none',
      background: 'transparent',
      color: '#e9eef6',
      fontSize: 10,
      padding: 0,
      opacity: searchDimmed ? 0.14 : 1,
    },
  }
}

function explorerFlowEdge(edge: BrainEdge, selected: string | null): Edge {
  const active = Boolean(selected && (edge.source === selected || edge.target === selected))
  const colour = STATUS_COLOUR[edge.status ?? 'proposed'] ?? '#748095'
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: active ? edge.label.replace(/[_-]+/g, ' ') : undefined,
    data: { claimId: edge.claimId },
    animated: edge.status === 'proposed' && active,
    style: {
      stroke: active ? colour : '#506075',
      opacity: active ? 0.92 : 0.32,
      strokeWidth: active ? 1.8 : 0.8,
      strokeDasharray: edge.status === 'proposed' ? '4 4' : undefined,
    },
    labelStyle: { fill: '#d4dbe5', fontSize: 9 },
    labelBgStyle: { fill: '#0d1117', fillOpacity: 0.9 },
  }
}

function categoryColour(category: AtlasCategory): string {
  return ({
    board: '#6e8fff', component: '#50c8a8', circuit: '#d596ff', datasheet: '#e8bf67',
    document: '#8fa0b8', software: '#65b6ff', measurement: '#ed8e73', requirement: '#c7a5ff', other: '#7c8798',
  })[category]
}

function uniqueEvidence(claims: BrainClaim[]) {
  const rows = new Map<string, BrainClaim['evidence'][number] & { key: string }>()
  for (const claim of claims) for (const evidence of claim.evidence) {
    const key = `${evidence.kind}:${evidence.sha256}:${evidence.page ?? ''}`
    rows.set(key, { ...evidence, key })
  }
  return [...rows.values()]
}

function toggle<T>(current: Set<T>, value: T): Set<T> {
  const next = new Set(current)
  if (next.has(value)) next.delete(value); else next.add(value)
  return next
}

function loadPositions(): Record<string, { x: number; y: number }> {
  try {
    const value = JSON.parse(window.localStorage.getItem(POSITION_KEY) ?? '{}')
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, { x: number; y: number }] => {
      const point = entry[1]
      return Boolean(point && typeof point === 'object' && typeof (point as { x?: unknown }).x === 'number' && typeof (point as { y?: unknown }).y === 'number')
    }))
  } catch { return {} }
}

function storePositions(value: Record<string, { x: number; y: number }>): void {
  try { window.localStorage.setItem(POSITION_KEY, JSON.stringify(value)) } catch { /* browser preference only */ }
}

function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {} }
function number(value: unknown, fallback: number): number { return typeof value === 'number' ? value : fallback }
function Row({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd title={value}>{value}</dd></div> }
function Metric({ label, value }: { label: string; value: number }) { return <div><strong>{value}</strong><span>{label}</span></div> }
