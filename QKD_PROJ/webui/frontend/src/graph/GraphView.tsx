/**
 * The mind-map. Every edge shows what it was read from, or it is not drawn.
 *
 * The one design rule worth stating: **the evidence is on screen, not in a
 * tooltip nobody opens.** An edge carries `evidence.field`, and the inspector
 * shows the field, the value and the ledger cursor — or "from a file" when
 * there is no cursor, because an edge derived from `contracts/*.yaml` does not
 * get to borrow one. A graph whose relationships have to be taken on trust is
 * the thing this project spends its effort not building.
 */

import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
  type NodeChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { GraphEdge, GraphNode, GraphSnapshot } from '../api/client'
import { NODE_HEIGHT, NODE_WIDTH, layout, type Placed } from './layout'
import type { LayoutApi } from './persistence'
import { useGraphLayout } from './useGraphLayout'

/** One colour per kind. Group nodes are deliberately the loudest. */
const PALETTE: Record<string, string> = {
  job: '#4c8dff',
  contract: '#c58cff',
  gate: '#8c7bff',
  brief: '#7f8c9b',
  agent: '#35c08a',
  approval: '#ffb23f',
  artifact: '#4fb8cf',
  run: '#e0715e',
  operation: '#f0c419',
  bitstream: '#a0a8b4',
}

const KINDS = Object.keys(PALETTE)

interface Props {
  snapshot: GraphSnapshot | null
  api: LayoutApi | null
}

export function GraphView({ snapshot, api }: Props) {
  const [placed, setPlaced] = useState<Record<string, Placed>>({})
  // Manual placement is persisted, so it outlives the tab. Everything else on
  // this screen is derived and can be thrown away; a position cannot.
  const manual = useGraphLayout(api)
  const pinned = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(manual.held).map(([id, at]) => [id, { id, x: at.x, y: at.y }]),
      ) as Record<string, Placed>,
    [manual.held],
  )
  const [selected, setSelected] = useState<string | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [laying, setLaying] = useState(false)

  const nodes = useMemo(
    () => (snapshot?.nodes ?? []).filter((n) => !hidden.has(n.kind)),
    [snapshot, hidden],
  )
  const visible = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes])
  const edges = useMemo(
    () =>
      (snapshot?.edges ?? []).filter((e) => visible.has(e.source) && visible.has(e.target)),
    [snapshot, visible],
  )

  // Re-laid out when the shape changes, not on every poll: a snapshot that
  // came back identical must not make the map jump under the user's cursor.
  const shape = useMemo(
    () => `${nodes.map((n) => n.id).join('|')}##${edges.map((e) => e.id).join('|')}`,
    [nodes, edges],
  )

  useEffect(() => {
    let cancelled = false
    setLaying(true)
    void layout(nodes, edges, pinned)
      .then((next) => {
        if (!cancelled) setPlaced(next)
      })
      .finally(() => {
        if (!cancelled) setLaying(false)
      })
    return () => {
      cancelled = true
    }
    // `pinned` is intentionally not a dependency: dragging a node should not
    // reflow the graph mid-drag. It is read on the next real shape change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shape])

  const flowNodes: Node[] = useMemo(
    () =>
      nodes.map((node) => {
        const at = pinned[node.id] ?? placed[node.id] ?? { id: node.id, x: 0, y: 0 }
        return {
          id: node.id,
          position: { x: at.x, y: at.y },
          data: { label: <NodeBody node={node} /> },
          style: {
            width: NODE_WIDTH,
            height: NODE_HEIGHT,
            borderRadius: 6,
            border: `1px solid ${PALETTE[node.kind] ?? '#888'}`,
            borderStyle: node.unresolved ? 'dashed' : 'solid',
            background: '#161a20',
            color: '#e6e9ee',
            fontSize: 11,
            padding: 0,
            boxShadow: selected === node.id ? `0 0 0 2px ${PALETTE[node.kind] ?? '#888'}` : 'none',
          },
        }
      }),
    [nodes, placed, pinned, selected],
  )

  const flowEdges: Edge[] = useMemo(
    () =>
      edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.evidence.field,
        animated: false,
        style: { stroke: '#3c4553' },
        labelStyle: { fill: '#8b94a3', fontSize: 9 },
        labelBgStyle: { fill: '#0f1216' },
      })),
    [edges],
  )

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === 'position' && change.position && !change.dragging) {
          // Dropped, not still moving. Saving mid-drag would be a PUT per pixel.
          manual.place(change.id, change.position.x, change.position.y)
        }
      }
    },
    [manual],
  )

  const chosen = snapshot?.nodes.find((n) => n.id === selected) ?? null

  return (
    <div className="graph">
      <div className="graph-canvas">
        {snapshot && snapshot.nodes.length === 0 ? (
          <Empty />
        ) : (
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            onNodesChange={onNodesChange}
            onNodeClick={(_, node) => setSelected(node.id)}
            onPaneClick={() => setSelected(null)}
            fitView
            minZoom={0.15}
            proOptions={{ hideAttribution: false }}
          >
            <Background color="#20262e" gap={22} />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(n) => {
                const kind = snapshot?.nodes.find((x) => x.id === n.id)?.kind ?? ''
                return PALETTE[kind] ?? '#888'
              }}
              style={{ background: '#12151a' }}
            />
          </ReactFlow>
        )}
      </div>

      <aside className="graph-side">
        <Legend
          hidden={hidden}
          counts={countKinds(snapshot)}
          onToggle={(kind) =>
            setHidden((current) => {
              const next = new Set(current)
              if (next.has(kind)) next.delete(kind)
              else next.add(kind)
              return next
            })
          }
        />
        <Inspector node={chosen} edges={snapshot?.edges ?? []} onPick={setSelected} />
        <div className="graph-foot">
          <span>
            {laying ? 'laying out…' : `cursor ${snapshot?.cursor ?? 0}`}
            {manual.saving && ' · saving'}
            {manual.error && <em className="warn"> · {manual.error}</em>}
          </span>
          {manual.moved > 0 && (
            <button className="link" onClick={manual.reset} disabled={manual.saving}>
              reset {manual.moved} moved
            </button>
          )}
        </div>
      </aside>
    </div>
  )
}

function NodeBody({ node }: { node: GraphNode }) {
  return (
    <div className="node">
      <div className="node-kind" style={{ color: PALETTE[node.kind] ?? '#888' }}>
        {node.kind}
        {node.unresolved && <span className="node-unresolved"> · unresolved</span>}
      </div>
      <div className="node-label" title={node.label}>
        {node.label}
      </div>
      {node.status && <div className="node-status">{node.status}</div>}
    </div>
  )
}

function Empty() {
  return (
    <div className="empty">
      <h3>Nothing on the ledger yet</h3>
      <p>
        The graph is derived, so it is empty until a scenario job exists.
      </p>
      <code>py -3 -m QKD_PROJ.orchestrator mission "&lt;scenario&gt;"</code>
    </div>
  )
}

function Legend({
  hidden,
  counts,
  onToggle,
}: {
  hidden: Set<string>
  counts: Record<string, number>
  onToggle: (kind: string) => void
}) {
  return (
    <section className="panel">
      <h3>Kinds</h3>
      <div className="legend">
        {KINDS.map((kind) => (
          <button
            key={kind}
            className={`chip ${hidden.has(kind) ? 'off' : ''}`}
            onClick={() => onToggle(kind)}
            title={hidden.has(kind) ? 'hidden — click to show' : 'click to hide'}
          >
            <i style={{ background: PALETTE[kind] }} />
            {kind}
            <b>{counts[kind] ?? 0}</b>
          </button>
        ))}
      </div>
    </section>
  )
}

function Inspector({
  node,
  edges,
  onPick,
}: {
  node: GraphNode | null
  edges: GraphEdge[]
  onPick: (id: string) => void
}) {
  if (!node) {
    return (
      <section className="panel">
        <h3>Inspector</h3>
        <p className="muted">Pick a node to see what it is joined to, and why.</p>
      </section>
    )
  }
  const out = edges.filter((e) => e.source === node.id)
  const inc = edges.filter((e) => e.target === node.id)

  return (
    <section className="panel">
      <h3>
        <span style={{ color: PALETTE[node.kind] ?? '#888' }}>{node.kind}</span> {node.label}
      </h3>
      {node.unresolved && (
        <p className="muted">
          The ledger named this, and nothing has filled in its details. It is kept rather than
          dropped — a reference that exists is not the same as one that does not.
        </p>
      )}
      {node.status && (
        <p className="muted">
          status <b>{node.status}</b> — from the control plane, not from an event
        </p>
      )}
      {Object.keys(node.metadata).length > 0 && (
        <dl className="meta">
          {Object.entries(node.metadata).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd title={String(value)}>{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <EdgeList title="Points at" rows={out} pick="target" onPick={onPick} />
      <EdgeList title="Pointed at by" rows={inc} pick="source" onPick={onPick} />
    </section>
  )
}

function EdgeList({
  title,
  rows,
  pick,
  onPick,
}: {
  title: string
  rows: GraphEdge[]
  pick: 'source' | 'target'
  onPick: (id: string) => void
}) {
  if (rows.length === 0) return null
  return (
    <div className="edges">
      <h4>{title}</h4>
      {rows.map((edge) => (
        <button key={edge.id} className="edge" onClick={() => onPick(edge[pick])}>
          <span className="edge-kind">{edge.kind}</span>
          <span className="edge-peer">{edge[pick]}</span>
          <span className="edge-why">
            {edge.evidence.field} = {shorten(edge.evidence.value)}
            {edge.evidence.source_event_id === null ? (
              <em title="read from a contract or a run manifest, not from a ledger row">
                {' '}
                from a file
              </em>
            ) : (
              <em> @{edge.evidence.source_event_id}</em>
            )}
          </span>
        </button>
      ))}
    </div>
  )
}

function shorten(value: string): string {
  return value.length > 24 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value
}

function countKinds(snapshot: GraphSnapshot | null): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const node of snapshot?.nodes ?? []) counts[node.kind] = (counts[node.kind] ?? 0) + 1
  return counts
}
