import { Background, Controls, Handle, MiniMap, Position, ReactFlow, useNodesState, useNodesInitialized, useReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { Api } from '../api/client'
import { knowledgeFailure, knowledgeSnapshot, readKnowledge } from './feed'
import { ATLAS_CATEGORIES, type AtlasCategory } from './atlas'
import { KnowledgeDetails, safeSourceUrl } from './KnowledgeDetails'
import { ConstellationMap } from './ConstellationMap'
import { TOPIC_ROOT } from './topics'
import { BRANCH_PAGE_SIZE, mindMapLayout, searchTopicClaims, topicBranch, topicForClaim, topicPath } from './mindmap'
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
const STATUS_LABEL: Record<string, string> = {verified: '근거 확인', proposed: '미검증', refuted: '반증', superseded: '대체됨'}
const POSITION_KEY = 'rtase.brain.positions.v3'
const NODE_TYPES = {mindmap: MindMapNode}

export function BrainView({ api }: { api: Api | null }) {
  const [events, setEvents] = useState<KnowledgeEvent[]>(() => knowledgeSnapshot(api).events)
  const [latest, setLatest] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [projectFilter, setProjectFilter] = useState('')
  const [statuses, setStatuses] = useState<Set<string>>(new Set(['verified', 'refuted', 'proposed']))
  const [categories, setCategories] = useState<Set<AtlasCategory>>(
    new Set(ATLAS_CATEGORIES.map(({ id }) => id)),
  )
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedClaimId, setSelectedClaimId] = useState<string | null>(null)
  const [mode, setMode] = useState<ExplorerMode>('topics')
  const [topicStyle, setTopicStyle] = useState<'constellation' | 'branches'>('constellation')
  const constellation = mode === 'topics' && topicStyle === 'constellation'
  const [topicFocus, setTopicFocus] = useState(TOPIC_ROOT)
  const [topicPage, setTopicPage] = useState(0)
  const [searchPage, setSearchPage] = useState(0)
  const [scope, setScope] = useState<'global' | 'local'>('global')
  const [depth, setDepth] = useState(1)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const savedPositions = useRef(loadPositions())

  const pending = useRef<AbortController | null>(null)
  const load = useCallback(async (background = false) => {
    if (!api) return
    if (pending.current) return
    const controller = new AbortController()
    pending.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 20_000)
    if (!background) setLoading(true)
    try {
      const snapshot = await readKnowledge(api, controller.signal)
      if (pending.current !== controller) return
      setEvents(snapshot.events)
      setLatest(snapshot.latest)
      setError('')
      if (snapshot.cursor < snapshot.latest) {
        setError(`이력 수신 중 ${snapshot.cursor}/${snapshot.latest} · 다음 조회에서 이어 받습니다.`)
      }
    } catch (cause) {
      if (pending.current !== controller) return
      const saved = knowledgeSnapshot(api)
      setEvents(saved.events)
      setLatest(saved.latest)
      setError(knowledgeFailure(cause))
    } finally {
      window.clearTimeout(timeout)
      if (pending.current === controller) {
        pending.current = null
        setLoading(false)
      }
    }
  }, [api])

  useEffect(() => {
    setEvents(knowledgeSnapshot(api).events)
    setLatest(knowledgeSnapshot(api).latest)
    void load()
    const timer = window.setInterval(() => { void load(true) }, 5_000)
    return () => { window.clearInterval(timer); pending.current?.abort(); pending.current = null }
  }, [api, load])

  const allKnowledge = useMemo(() => foldKnowledgeEvents(events), [events])
  const projectIds = useMemo(() => [...new Set(allKnowledge.claims.map(c => c.scope.project_id || '공통'))].sort(), [allKnowledge])
  const projectNames = useMemo(() => new Map(allKnowledge.claims.flatMap(c => {
    const data = c.object.kind === 'literal' && typeof c.object.value === 'object' && c.object.value !== null ? c.object.value as {project?: {name?: string}} : {}
    return c.scope.project_id && typeof data.project?.name === 'string' ? [[c.scope.project_id, data.project.name] as const] : []
  })), [allKnowledge])
  const source = useMemo(() => ({...allKnowledge, claims: projectFilter
    ? allKnowledge.claims.filter(c => (c.scope.project_id || '공통') === projectFilter) : allKnowledge.claims}), [allKnowledge, projectFilter])
  const complete = useMemo(() => buildExplorerGraph(mode === 'topics'
    ? {...source, claims: source.claims.filter(c => statuses.has(c.status))} : source, mode), [source, mode, statuses])
  const filtered = useMemo(
    () => mode === 'topics' ? complete : filterExplorerGraph(complete, categories, statuses),
    [complete, mode, categories, statuses],
  )
  const branch = useMemo(() => mode === 'topics' ? topicBranch(filtered, topicFocus, topicPage) : null,
    [filtered, mode, topicFocus, topicPage])
  const path = useMemo(() => branch ? topicPath(complete, branch.focus.id) : [], [complete, branch])
  const searchResults = useMemo(() => searchTopicClaims(complete, query), [complete, query])
  const shown = useMemo(
    () => branch ? branch.graph : scope === 'local' && selectedNodeId
      ? localExplorerGraph(filtered, selectedNodeId, depth)
      : filtered,
    [branch, filtered, scope, selectedNodeId, depth],
  )
  const matches = useMemo(() => matchingExplorerNodeIds(filtered, query), [filtered, query])
  const focusIds = useMemo(() => new Set(selectedNodeId
    ? [selectedNodeId, ...shown.edges.filter(e => e.source === selectedNodeId || e.target === selectedNodeId)
      .flatMap(e => [e.source, e.target])] : []), [shown.edges, selectedNodeId])
  const layout = useMemo(() => mode === 'topics' ? mindMapLayout(shown) : forceExplorerLayout(shown), [shown, mode])
  const selectedNode = complete.nodes.find((node) => node.id === selectedNodeId) ?? null
  const selectedClaims = useMemo(
    () => selectedNodeId ? claimsForNode(complete, selectedNodeId) : [],
    [complete, selectedNodeId],
  )
  const selectedClaim = selectedClaims.find((claim) => claim.claimId === selectedClaimId)
    ?? (mode === 'topics' ? null : selectedClaims[0])
    ?? null
  const connections = useMemo(
    () => selectedNodeId ? connectionsForNode(complete, selectedNodeId) : [],
    [complete, selectedNodeId],
  )
  const initialNodes = useMemo(() => shown.nodes.map((node) => explorerFlowNode(
    node,
    (mode === 'topics' ? undefined : savedPositions.current[node.id]) ?? layout.get(node.id) ?? { x: 0, y: 0 },
    mode === 'topics' && selectedClaimId ? `topic-record:${selectedClaimId}` : selectedNodeId,
    matches,
    query,
    focusIds,
    mode === 'topics',
  )), [shown.nodes, layout, selectedNodeId, selectedClaimId, matches, query, focusIds, mode])
  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node>(initialNodes)

  useEffect(() => {
    setFlowNodes(initialNodes)
  }, [initialNodes, setFlowNodes])

  useEffect(() => {
    if (selectedNodeId && !complete.nodes.some((node) => node.id === selectedNodeId)) {
      setSelectedNodeId(null)
      setSelectedClaimId(null)
      setScope('global')
    }
  }, [complete.nodes, selectedNodeId])

  const flowEdges: Edge[] = useMemo(() => shown.edges.map((edge) => {
    const value = explorerFlowEdge(edge, selectedNodeId)
    if (mode !== 'topics') return value
    const right = (layout.get(edge.target)?.x ?? 0) > (layout.get(edge.source)?.x ?? 0)
    return {...value, sourceHandle: right ? 'source-right' : 'source-left', targetHandle: right ? 'target-left' : 'target-right'}
  }), [shown.edges, selectedNodeId, mode, layout])

  const selectNode = useCallback((node: ExplorerNode) => {
    if (mode === 'topics') {
      if (node.navigation) {
        setTopicFocus(node.id)
        setTopicPage(0)
        setSelectedNodeId(node.id)
        setSelectedClaimId(null)
        setInspectorOpen(constellation)
      } else {
        setSelectedNodeId(branch?.focus.id ?? TOPIC_ROOT)
        setSelectedClaimId(node.claimId ?? null)
        setInspectorOpen(true)
      }
      setQuery('')
      return
    }
    setSelectedNodeId(node.id)
    setSelectedClaimId(node.claimIds[0] ?? null)
    setInspectorOpen(true)
  }, [mode, branch, constellation])

  const changeMode = (value: ExplorerMode) => {
    setMode(value)
    setSelectedNodeId(null)
    setSelectedClaimId(null)
    setScope('global')
  }

  const resetLayout = useCallback(() => {
    savedPositions.current = {}
    window.localStorage.removeItem(POSITION_KEY)
    setFlowNodes(shown.nodes.map((node) => explorerFlowNode(
      node,
      layout.get(node.id) ?? { x: 0, y: 0 },
      selectedNodeId,
      matches,
      query,
      focusIds,
    )))
  }, [layout, matches, query, selectedNodeId, setFlowNodes, shown.nodes, focusIds])

  return (
    <div className={`brain obsidian-brain ${mode === 'topics' ? 'mindmap-brain' : ''}`}>
      <div className="brain-toolbar obsidian-toolbar">
        <label className="brain-search">
          <span>⌕</span>
          <input
            value={query}
            onChange={(event) => { setQuery(event.target.value); setSearchPage(0); setInspectorOpen(true) }}
            placeholder="접힌 가지까지 검색 · 보드, 기능, 문제…"
          />
          {query && <b>{mode === 'topics' ? searchResults.length : matches.size}</b>}
        </label>
        <label className="brain-project-filter">프로젝트
          <select aria-label="지식 프로젝트 필터" value={projectFilter} onChange={event => {
            setProjectFilter(event.target.value); setSelectedNodeId(null); setSelectedClaimId(null); setTopicFocus(TOPIC_ROOT); setTopicPage(0)
          }}><option value="">전체 프로젝트</option>{projectIds.map(id=><option key={id} value={id}>{projectNames.get(id) || id}</option>)}</select>
        </label>
        <div className="brain-toolbar-group">
          <button className={`chip ${constellation ? 'on' : 'off'}`} onClick={() => { changeMode('topics'); setTopicStyle('constellation') }}>전체 지식맵</button>
          <button className={`chip ${mode === 'topics' && !constellation ? 'on' : 'off'}`} onClick={() => { changeMode('topics'); setTopicStyle('branches') }}>가지별 보기</button>
          <button className={`chip ${mode === 'concepts' ? 'on' : 'off'}`} onClick={() => changeMode('concepts')}>원본 관계</button>
          <button className={`chip ${mode === 'evidence' ? 'on' : 'off'}`} onClick={() => changeMode('evidence')}>근거 상세</button>
        </div>
        {mode !== 'topics' && <div className="brain-toolbar-group">
          <button className={`chip ${scope === 'global' ? 'on' : 'off'}`} onClick={() => setScope('global')}>Global</button>
          <button
            className={`chip ${scope === 'local' ? 'on' : 'off'}`}
            disabled={!selectedNodeId}
            onClick={() => setScope('local')}
          >Local</button>
          {scope === 'local' && [1, 2, 3].map((value) => (
            <button key={value} className={`chip depth ${depth === value ? 'on' : 'off'}`} onClick={() => setDepth(value)}>{value}</button>
          ))}
        </div>}
        {mode !== 'topics' && <button className="link" onClick={resetLayout}>reflow</button>}
        <button className="link" disabled={loading || !api} onClick={() => void load()}>{loading ? 'reading…' : 'refresh'}</button>
      </div>

      {(error || !api) && <div className="brain-connection-notice" role="status">{error || '현재 세션 주소로 웹에 연결해야 저장된 지식을 읽을 수 있습니다.'}{events.length > 0 && ' · 마지막 수신 자료를 표시 중입니다.'}</div>}
      {mode === 'topics' && <>
        <div className="brain-topic-help">{constellation
          ? '드래그로 이동 · 휠로 확대 · 점을 눌러 내용 확인 · 색은 주제, 선은 지식 분류를 뜻합니다.'
          : '가지를 누르면 하위 항목, 기록을 누르면 내용과 근거가 열립니다. 선은 탐색용 분류이며 회로 연결을 뜻하지 않습니다.'}</div>
        {!constellation && <nav className="brain-breadcrumb" aria-label="지식맵 탐색 경로">
          {path.map((node, index) => <span key={node.id}>
            {index > 0 && <span aria-hidden="true"> › </span>}
            <button aria-current={index === path.length - 1 ? 'location' : undefined} onClick={() => selectNode(node)}>{node.label}</button>
          </span>)}
          {branch && branch.pages > 1 && <PageButtons page={branch.page} pages={branch.pages} onChange={setTopicPage} />}
        </nav>}
      </>}
      <details className="brain-filters" open={mode !== 'topics'}><summary>분류·검증 상태 필터</summary><div className="brain-filterbar">
        {mode !== 'topics' && <span className="filter-title">Types</span>}
        {mode !== 'topics' && ATLAS_CATEGORIES.map(({ id, label }) => {
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
      </div></details>

      <div className={`brain-body obsidian-body ${inspectorOpen ? 'inspector-open' : ''}`}>
        <div className="brain-canvas obsidian-canvas">
          {constellation ? <ConstellationMap graph={complete} query={query}
            selectedId={selectedClaimId ? `star-record:${selectedClaimId}` : selectedNodeId}
            onSelect={node => {
              if (node.navigation) selectNode(node)
              else {
                setSelectedNodeId(node.parentId ?? TOPIC_ROOT)
                setSelectedClaimId(node.claimId ?? null)
                setQuery('')
                setInspectorOpen(true)
              }
            }} /> : events.length === 0 && mode !== 'topics' ? (
            <div className="empty">
              <h3>{error ? 'Knowledge is unavailable' : 'The durable brain is empty'}</h3>
              <p>{error || 'Research, plans, runs and verified measurements will accumulate here across jobs.'}</p>
            </div>
          ) : shown.nodes.length === 0 ? (
            <div className="empty"><h3>No notes match these filters</h3><p>Turn a type or truth filter back on.</p></div>
          ) : (
            <ReactFlow
              nodeTypes={NODE_TYPES}
              nodes={flowNodes}
              edges={flowEdges}
              onNodesChange={onNodesChange}
              fitView
              minZoom={0.08}
              maxZoom={2.4}
              nodesDraggable={mode !== 'topics'}
              nodesConnectable={false}
              onNodeClick={(_, node) => {
                const found = shown.nodes.find((candidate) => candidate.id === node.id)
                if (found) selectNode(found)
              }}
              onNodeDoubleClick={(_, node) => {
                if (mode === 'topics') return
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
                if (typeof claimId === 'string') { setSelectedClaimId(claimId); setInspectorOpen(true) }
              }}
              onPaneClick={() => {
                if (mode === 'topics') return
                if (scope === 'global') {
                  setSelectedNodeId(null)
                  setSelectedClaimId(null)
                }
              }}
              proOptions={{ hideAttribution: false }}
            >
              <FitKnowledge topology={shown.nodes.map(n => n.id).join('|')} />
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
            {constellation ? '전체 지식' : branch ? `${branch.focus.label} · ${branch.total}개 하위 항목` : scope === 'local' && selectedNode
              ? `Local graph · ${selectedNode.label} · depth ${depth}`
              : 'Global graph'}
            {!constellation && <span>{shown.nodes.length} notes · {shown.edges.length} links</span>}
            {mode === 'topics' && <span>{complete.claims.length}개 기록{events.length === 0 ? ' · 분류만 표시 (아직 수신한 자료 없음)' : ''}</span>}
          </div>
          <button className="brain-note-toggle" onClick={() => setInspectorOpen(true)}>기록 패널</button>
        </div>

        <aside className="brain-side obsidian-side">
          <button className="brain-note-close" onClick={() => setInspectorOpen(false)}>× close</button>
          {error && events.length > 0 && <p className="warn">{error}</p>}
          <p className="muted small brain-cursor">knowledge cursor {source.cursor}/{latest}</p>
          {mode === 'topics' && query.trim() ? (
            <TopicSearch graph={complete} results={searchResults} page={searchPage} onPage={setSearchPage} onSelect={(claim) => {
              const parent = topicForClaim(complete, claim.claimId)
              if (!parent) return
              selectNode(parent)
              const records = complete.claims.filter(c => parent.claimIds.includes(c.claimId))
              setTopicPage(Math.floor(records.findIndex(c => c.claimId === claim.claimId) / BRANCH_PAGE_SIZE))
              setSelectedClaimId(claim.claimId)
              setInspectorOpen(true)
            }} />
          ) : selectedNode ? (
            <NoteInspector key={selectedNode.id}
              node={selectedNode}
              claims={selectedClaims}
              selectedClaim={selectedClaim}
              connections={connections}
              onSelectNode={(id) => {
                const found = complete.nodes.find((node) => node.id === id)
                if (found) selectNode(found)
              }}
              onSelectClaim={(id) => { setSelectedClaimId(id); setInspectorOpen(true) }}
            />
          ) : (
            <BrainOverview graph={complete} matches={matches} query={query} onSelect={selectNode} />
          )}
        </aside>
      </div>
    </div>
  )
}

function FitKnowledge({ topology }: { topology: string }) {
  const initialized = useNodesInitialized()
  const { fitView } = useReactFlow()
  useEffect(() => {
    if (initialized) void fitView({padding: 0.12, duration: 250})
  }, [topology, initialized, fitView])
  return null
}

function MindMapNode({data}: NodeProps) {
  return <>
    {(['left', 'right'] as const).flatMap(side => (['source', 'target'] as const).map(type =>
      <Handle key={`${type}-${side}`} id={`${type}-${side}`} type={type} position={side === 'left' ? Position.Left : Position.Right} isConnectable={false} />,
    ))}
    {data.label as ReactNode}
  </>
}

function PageButtons({ page, pages, onChange }: {page: number; pages: number; onChange: (page: number) => void}) {
  return <span className="brain-pages">
    <button disabled={page === 0} onClick={() => onChange(page - 1)} aria-label="이전 항목">←</button>
    <span>{page + 1} / {pages}</span>
    <button disabled={page + 1 >= pages} onClick={() => onChange(page + 1)} aria-label="다음 항목">→</button>
  </span>
}

function TopicSearch({graph, results, page, onPage, onSelect}: {
  graph: ExplorerGraph; results: BrainClaim[]; page: number; onPage: (page: number) => void; onSelect: (claim: BrainClaim) => void
}) {
  const pages = Math.max(1, Math.ceil(results.length / BRANCH_PAGE_SIZE))
  const current = Math.min(page, pages - 1)
  return <section className="brain-note">
    <header><h2>검색 결과 · {results.length}</h2></header>
    <p className="note-lead">접힌 가지의 기록도 검색합니다. 선택하면 해당 분류와 근거가 열립니다.</p>
    {!results.length && <p>일치하는 기록이 없습니다. 검색어나 검증 상태 필터를 확인하세요.</p>}
    <div className="claim-list">{results.slice(current * BRANCH_PAGE_SIZE, (current + 1) * BRANCH_PAGE_SIZE).map(claim => {
      const parent = topicForClaim(graph, claim.claimId)
      return <button key={claim.claimId} onClick={() => onSelect(claim)}>
        <i style={{background: STATUS_COLOUR[claim.status]}} />
        <span>{conceptLabel(claim.statement, 15, 160)}<small>{parent && topicPath(graph, parent.id).map(n => n.label).join(' › ')}</small></span>
      </button>
    })}</div>
    {pages > 1 && <PageButtons page={current} pages={pages} onChange={onPage} />}
  </section>
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
      <header><small>개발 지식</small><h2>{query ? '검색 결과' : '분류에서 시작하세요'}</h2></header>
      <p className="note-lead">
        {query
          ? `${matches.size} notes mention “${query}”. Select one to open its local graph and backlinks.`
          : '가지를 눌러 보드·기술을 좁혀 들어가고, 세부 기록에서 내용과 근거를 확인하세요.'}
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
  const [claimPage, setClaimPage] = useState(0)
  const incoming = connections.filter((connection) => connection.direction === 'in')
  const outgoing = connections.filter((connection) => connection.direction === 'out')
  const evidence = uniqueEvidence(selectedClaim ? [selectedClaim] : [])
  const jobs = selectedClaim?.sourceJobId ? [selectedClaim.sourceJobId] : []
  const pages = Math.max(1, Math.ceil(claims.length / BRANCH_PAGE_SIZE))
  const page = Math.min(claimPage, pages - 1)
  return (
    <section className="brain-note">
      <header>
        <div className="note-kicker"><i style={{ background: categoryColour(node.category) }} />{node.category} · {node.kind}</div>
        <h2>{node.label}</h2>
        <div className="note-badges">
          {node.status && <span style={{ borderColor: STATUS_COLOUR[node.status], color: STATUS_COLOUR[node.status] }}>{node.status}</span>}
          <span>{node.degree} links</span>
          <span>{claims.length}개 기록</span>
        </div>
      </header>

      {node.navigation && <p className="note-lead">관련 기록을 묶은 탐색용 분류입니다. 아래 기록마다 검증 상태와 출처를 확인하세요.</p>}

      {selectedClaim && (
        <div className="note-summary">
          <small style={{color: STATUS_COLOUR[selectedClaim.status]}}>{STATUS_LABEL[selectedClaim.status] ?? selectedClaim.status}</small>
          <KnowledgeDetails claim={selectedClaim} />
          <dl className="note-fact">
            <Row label="대상" value={selectedClaim.subject.label} />
            {selectedClaim.object.kind === 'entity' && <Row label="연결 대상" value={objectLabel(selectedClaim.object)} />}
            {selectedClaim.scope.hardware_id && <Row label="보드·리비전" value={[selectedClaim.scope.hardware_id, selectedClaim.scope.revision].filter(Boolean).join(' · ')} />}
            <Row label="작성" value={selectedClaim.producer} />
            <Row label="갱신" value={selectedClaim.at} />
          </dl>
          <p className="muted small">근거 확인은 저장 시점의 자료 검사 결과입니다. 현재 장비의 동작 성공을 뜻하지 않습니다.</p>
        </div>
      )}

      <ConnectionList title={node.navigation ? '하위 분류' : '연결'} rows={outgoing} onSelect={onSelectNode} />
      <ConnectionList title={node.navigation ? '상위 분류' : '역방향 연결'} rows={incoming} onSelect={onSelectNode} />

      {claims.length > 0 && (
        <details className="note-details" open={!selectedClaim}>
          <summary>이 분류의 기록 · {claims.length}</summary>
          <div className="claim-list">
            {claims.slice(page * BRANCH_PAGE_SIZE, (page + 1) * BRANCH_PAGE_SIZE).map((claim) => (
              <button key={claim.claimId} className={selectedClaim?.claimId === claim.claimId ? 'selected' : ''} onClick={() => onSelectClaim(claim.claimId)}>
                <i style={{ background: STATUS_COLOUR[claim.status] }} />
                <span>{conceptLabel(claim.statement, 8, 90)}<small>{claim.relation.replace(/[_-]+/g, ' ')}</small></span>
              </button>
            ))}
          </div>
          {pages > 1 && <PageButtons page={page} pages={pages} onChange={setClaimPage} />}
        </details>
      )}

      {selectedClaim && <><h3>선택한 기록의 근거 <b>{evidence.length}</b></h3>
      {evidence.length === 0 ? <p className="muted small">첨부된 근거가 없습니다.</p> : (
        <div className="source-list">{evidence.map((item) => (
          <article key={item.key}>
            <strong>{item.role} · {item.kind}{item.page ? ` · p.${item.page}` : ''}</strong>
            {safeSourceUrl(item.locator) ? <a href={safeSourceUrl(item.locator)!} target="_blank" rel="noreferrer">{item.locator}</a> : <span>{item.locator}</span>}
            <code>{item.sha256.slice(0, 16)}…</code>
            {item.excerpt && <q>{item.excerpt}</q>}
          </article>
        ))}</div>
      )}</>}

      {jobs.length > 0 && <><h3>관련 작업</h3><div className="job-links">{jobs.map((job) => <span key={job}>↳ {job}</span>)}</div></>}
      {selectedClaim && <details className="note-details"><summary>기록 식별자·적용 범위·검사 내역</summary><code>{selectedClaim.claimId}</code><pre>{JSON.stringify({scope: selectedClaim.scope, checks: selectedClaim.checks}, null, 2)}</pre></details>}
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
  focusIds: Set<string>,
  mindmap = false,
): Node {
  const colour = categoryColour(node.category)
  const root = node.navigation && node.id.split(':').length === 2
  const size = root ? 24 : node.navigation ? 12 : Math.min(22, 10 + Math.sqrt(Math.max(node.degree, 1)) * 3)
  const searchDimmed = query.trim().length > 0 ? !matches.has(node.id) : focusIds.size > 0 && !focusIds.has(node.id)
  const active = selected === node.id
  if (mindmap) return {
    id: node.id, type: 'mindmap', position,
    data: {colour, label: <div className={`mindmap-card ${active ? 'selected' : ''}`} style={{borderLeftColor: colour}} title={node.label}>
      <strong>{node.navigation ? node.label : conceptLabel(node.label, 18, 110)}</strong>
      <small>{node.navigation ? `${node.claimIds.length}개 기록 · 눌러서 탐색 ›` : `${STATUS_LABEL[node.status ?? 'proposed']} · 내용과 근거 보기`}</small>
    </div>},
    style: {width: 240, height: 86, padding: 0, border: 'none', background: 'transparent'},
  }
  return {
    id: node.id,
    position,
    data: {
      colour,
      claimIds: node.claimIds,
      label: (
        <div className={`obsidian-node ${node.navigation ? 'topic-node' : ''} ${root ? 'topic-root' : ''} ${active ? 'selected' : ''} ${searchDimmed ? 'dimmed' : ''}`} title={node.label}>
          <i
            className="obsidian-orb"
            style={{
              width: size,
              height: size,
              background: colour,
              borderColor: node.navigation ? colour : STATUS_COLOUR[node.status ?? 'proposed'],
            }}
          />
          <span>{conceptLabel(node.label, 4, 38)}</span>
          {root && <small>{node.claimIds.length}개 기록</small>}
        </div>
      ),
    },
    style: {
      width: 128,
      height: root ? 72 : 54,
      border: 'none',
      background: 'transparent',
      color: '#e9eef6',
      fontSize: node.navigation ? 14 : 10,
      padding: 0,
      opacity: searchDimmed ? 0.14 : 1,
    },
  }
}

function explorerFlowEdge(edge: BrainEdge, selected: string | null): Edge {
  const active = Boolean(selected && (edge.source === selected || edge.target === selected))
  const navigation = edge.id.startsWith('topic-link:')
  const colour = navigation ? '#9d8dff' : STATUS_COLOUR[edge.status ?? 'proposed'] ?? '#748095'
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: navigation ? 'default' : 'straight',
    label: active && !navigation ? edge.label.replace(/[_-]+/g, ' ') : undefined,
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

function Row({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd title={value}>{value}</dd></div> }
function Metric({ label, value }: { label: string; value: number }) { return <div><strong>{value}</strong><span>{label}</span></div> }
