/** Navigation over stored claims, not new scientific claims or circuit links. */
import type { ExplorerGraph, ExplorerNode } from './explorer'
import type { AtlasCategory } from './atlas'
import { objectLabel, type BrainGraph, type BrainEdge } from './model'

const ROOTS: [AtlasCategory, string][] = [
  ['board', '보드'], ['component', '부품'], ['circuit', '회로·인터페이스'],
  ['software', '소프트웨어·구현'], ['measurement', '실행·검증'], ['document', '문서·자료'],
]
const TOPICS: [string, AtlasCategory, RegExp][] = [
  ['BBB', 'board', /\bBBB\b|BeagleBone/i],
  ['STM32', 'board', /\bSTM32[a-z0-9]*\b|\bNucleo\b/i],
  ['RTASE V1', 'board', /RTASE[ _-]*V?1/i],
  ['CPLD', 'component', /\bCPLD\b|MachXO2|LCMXO2/i],
  ['FPGA', 'component', /\bFPGA\b|Cyclone|Artix/i],
  ['DAC', 'component', /\bDAC\d*\b|digital.to.analog/i],
  ['ADC', 'component', /\bADC\d*\b/i],
  ['Oscillator', 'component', /oscillator|oscillation|오실레이터|발진기/i],
  ['Power', 'circuit', /power.supply|power.rail|regulator|전원부|전원 레일/i],
  ['SPI', 'circuit', /\bSPI\b|GPIO.SPI/i],
  ['GPIO', 'circuit', /\bGPIO\b/i],
  ['JTAG', 'circuit', /\bJTAG\b/i],
  ['RF1 · RF2', 'circuit', /\bRF[12]\b/i],
  ['PRU', 'software', /\bPRU[01]?\b/i],
  ['DMA', 'software', /\b(?:E?DMA)\b/i],
  ['HRTIM', 'software', /\bHRTIM\w*\b|\bHRTIMER\b|high.resolution.timer/i],
  ['Firmware', 'software', /firmware|펌웨어/i],
  ['Diamond', 'software', /\bDiamond\b/i],
  ['RTM3004', 'measurement', /\bRTM3004\b/i],
  ['SCPI', 'measurement', /\bSCPI\b/i],
  ['Waveform', 'measurement', /waveform|파형/i],
]
export const TOPIC_ROOT = 'topic:root'
const PROCEDURE = new Set(['assigns_agent', 'assigns_skill', 'proposes_contract_action', 'requires_material'])
const FEATURES = new Set(['PRU', 'DMA', 'HRTIM', 'GPIO', 'SPI', 'Firmware'])

/** Presentation-only tree. Scope is kept in board identity; no DB facts are merged. */
export function buildTopicGraph(source: BrainGraph): ExplorerGraph {
  const nodes = new Map<string, ExplorerNode>()
  const edges = new Map<string, BrainEdge>()
  const node = (id: string, label: string, category: AtlasCategory, claimId?: string) => {
    let value = nodes.get(id)
    if (!value) {
      value = { id, label, category, kind: 'entity', claimIds: [], degree: 0,
        navigation: true, searchText: `${label} ${category}`.toLowerCase() }
      nodes.set(id, value)
    }
    if (claimId && !value.claimIds.includes(claimId)) value.claimIds.push(claimId)
    return value
  }
  const link = (parent: string, child: string) => {
    const id = `topic-link:${parent}:${child}`
    edges.set(id, {id, source: parent, target: child, label: '분류 · 관련 기록'})
  }
  const branch = (parent: string, id: string, label: string, category: AtlasCategory, claimId: string) => {
    node(id, label, category, claimId)
    link(parent, id)
    return id
  }
  node(TOPIC_ROOT, '개발 지식', 'other')
  for (const [category, label] of ROOTS) {
    node(`topic:${category}`, label, category)
    link(TOPIC_ROOT, `topic:${category}`)
  }
  for (const claim of source.claims) {
    // Old assignments and contract workflow remain searchable in a dedicated
    // branch; their board keywords must not overwhelm engineering knowledge.
    if (PROCEDURE.has(claim.relation)) {
      branch('topic:document', 'topic:document:history', '과거 절차 기록', 'document', claim.claimId)
      continue
    }
    if (claim.scope.project_id) {
      const id = `topic:projects:${encodeURIComponent(claim.scope.project_id)}`
      node('topic:projects', '프로젝트', 'other')
      link(TOPIC_ROOT, 'topic:projects')
      const data = claim.object.kind === 'literal' && claim.object.value && typeof claim.object.value === 'object' ? claim.object.value as Record<string, unknown> : {}
      const project = data.project as {name?: string} | undefined
      branch('topic:projects', id, project?.name || claim.scope.project_id, 'other', claim.claimId)
    }
    const text = [claim.subject.label, claim.statement, objectLabel(claim.object), ...claim.tags].join(' ')
    const found = TOPICS.filter(([, , pattern]) => pattern.test(text))
    const scopeKey = encodeURIComponent(JSON.stringify([
      claim.scope.project_id ?? '', claim.scope.hardware_id ?? '', claim.scope.revision ?? '',
    ]))
    const entities = [claim.subject, ...(claim.object.kind === 'entity' ? [claim.object.entity] : [])]
    const explicitBoards = entities.filter(entity => entity.kind === 'board')
    const boardParents: string[] = []
    // A board entity or stored hardware scope works for any model, even when
    // the short topic vocabulary has never heard of it.
    if (explicitBoards.length) {
      for (const board of explicitBoards) {
        boardParents.push(branch('topic:board', `topic:board:entity:${scopeKey}:${encodeURIComponent(board.id)}`,
          [board.label, claim.scope.revision].filter(Boolean).join(' · '), 'board', claim.claimId))
      }
    } else if (claim.scope.hardware_id) {
      boardParents.push(branch('topic:board', `topic:board:scope:${scopeKey}`,
        [claim.scope.hardware_id, claim.scope.revision].filter(Boolean).join(' · '), 'board', claim.claimId))
    } else {
      for (const [label, category] of found.filter(([, category]) => category === 'board')) {
        boardParents.push(branch('topic:board', `topic:board:${label}`, label, category, claim.claimId))
      }
    }
    for (const [label, category] of found) {
      if (category === 'board') continue
      // Board features are also discoverable through the shared technology
      // category. Those lines mean co-classification, never physical wiring.
      branch(`topic:${category}`, `topic:${category}:${label}`, label, category, claim.claimId)
      if (FEATURES.has(label) || category === 'circuit' || category === 'measurement') {
        for (const parent of boardParents) branch(parent, `${parent}:${label}`, label, 'board', claim.claimId)
      }
    }
    for (const entity of entities) {
      if (!['component', 'instrument', 'software'].includes(entity.kind)) continue
      const category = entity.kind === 'instrument' ? 'measurement' : entity.kind as AtlasCategory
      branch(`topic:${category}`, `topic:${category}:entity:${scopeKey}:${encodeURIComponent(entity.id)}`,
        entity.label, category, claim.claimId)
    }
    if (claim.tags.includes('conversation')) {
      for (const parent of boardParents) branch(parent,
        `${parent}:result:${encodeURIComponent(claim.subject.id)}`, claim.subject.label, 'board', claim.claimId)
      if (claim.relation === 'device_observation') branch('topic:measurement',
        `topic:measurement:tool:${encodeURIComponent(claim.subject.id)}`, claim.subject.label, 'measurement', claim.claimId)
    }
    // Source categories collect referenced records, not invented source nodes.
    if (claim.evidence.length || /datasheet|manual|document|회로도|데이터시트/i.test(text)) {
      const datasheet = /datasheet|데이터시트/i.test(text)
      branch('topic:document', datasheet ? 'topic:document:datasheet' : 'topic:document:sources',
        datasheet ? '데이터시트' : '참고 자료', 'document', claim.claimId)
    }
    if (claim.relation.startsWith('finding_') && claim.relation !== 'finding_unchanged') {
      for (const parent of boardParents.length ? boardParents : ['topic:software']) {
        branch(parent, `${parent}:issues`, '문제·변경 사항', boardParents.length ? 'board' : 'software', claim.claimId)
      }
    }
    // Every record remains reachable, even with no recognizable noun or source.
    if (![...nodes.values()].some(n => n.claimIds.includes(claim.claimId))) {
      branch('topic:document', 'topic:document:uncategorized', '기타 기록', 'document', claim.claimId)
    }
  }
  // Some board records have no feature keyword. Keep them in a visible leaf
  // when sibling features exist, rather than burying them in the parent count.
  for (const n of [...nodes.values()]) {
    const children = [...edges.values()].filter(e => e.source === n.id).map(e => nodes.get(e.target)!)
    if (!children.length || !n.claimIds.length) continue
    const covered = new Set(children.flatMap(child => child.claimIds))
    for (const id of n.claimIds.filter(id => !covered.has(id))) {
      branch(n.id, `${n.id}:general`, '개요·기타 기록', n.category, id)
    }
  }
  // Propagate unique records from leaves for honest counts and whole-branch search.
  const parents = new Map([...edges.values()].map(e => [e.target, e.source]))
  for (const n of nodes.values()) {
    let parent = parents.get(n.id)
    while (parent) {
      for (const id of n.claimIds) node(parent, '', nodes.get(parent)!.category, id)
      parent = parents.get(parent)
    }
  }
  const claims = new Map(source.claims.map(c => [c.claimId, c]))
  for (const edge of edges.values()) {
    nodes.get(edge.source)!.degree++
    nodes.get(edge.target)!.degree++
  }
  for (const n of nodes.values()) {
    n.claimIds.sort()
    n.searchText += ' ' + n.claimIds.map(id => {
      const c = claims.get(id)!
      return [c.statement, c.subject.label, objectLabel(c.object), ...Object.values(c.scope), ...c.tags].join(' ')
    }).join(' ').toLowerCase()
  }
  return {nodes: [...nodes.values()], edges: [...edges.values()], claims: source.claims, cursor: source.cursor}
}
