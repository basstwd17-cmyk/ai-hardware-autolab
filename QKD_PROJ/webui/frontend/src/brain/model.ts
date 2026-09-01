export interface KnowledgeEvent {
  event_id: number
  at: string
  kind: string
  claim_id: string
  version_id: string
  payload: Record<string, unknown>
}

export interface BrainClaim {
  claimId: string
  versionId: string
  status: string
  eventId: number
  at: string
  producer: string
  scope: Record<string, string>
  subject: BrainEntity
  relation: string
  object: BrainObject
  statement: string
  evidence: BrainEvidence[]
  checks: Array<Record<string, unknown>>
  sourceJobId: string
  tags: string[]
}

export interface BrainEntity { kind: string; id: string; label: string }
export type BrainObject =
  | { kind: 'entity'; entity: BrainEntity }
  | { kind: 'literal'; value: unknown; unit?: string }
export interface BrainEvidence {
  kind: string
  role: string
  locator: string
  sha256: string
  page?: number
  excerpt?: string
}

export interface BrainNode {
  id: string
  kind: 'entity' | 'claim' | 'literal' | 'evidence' | 'job'
  label: string
  status?: string
  claimId?: string
}
export interface BrainEdge {
  id: string
  source: string
  target: string
  label: string
  claimId?: string
  status?: string
}
export interface BrainGraph { claims: BrainClaim[]; nodes: BrainNode[]; edges: BrainEdge[]; cursor: number }

export function foldKnowledgeEvents(events: KnowledgeEvent[]): BrainGraph {
  const current = new Map<string, BrainClaim>()
  let cursor = 0
  const seen = new Set<number>()
  for (const event of [...events].sort((a, b) => a.event_id - b.event_id)) {
    if (seen.has(event.event_id)) continue
    seen.add(event.event_id)
    cursor = Math.max(cursor, event.event_id)
    if (event.kind === 'knowledge.superseded') {
      const old = String(event.payload.old_claim_id ?? event.claim_id)
      const existing = current.get(old)
      if (existing) current.set(old, { ...existing, status: 'superseded', eventId: event.event_id, at: event.at })
      continue
    }
    const claim = parseClaim(event)
    if (!claim) continue
    const existing = current.get(claim.claimId)
    // Mirror KnowledgeStore.record(): a later unverified proposal is retained
    // in history but never downgrades an already verified/refuted projection.
    if (!existing || (existing.status !== 'superseded' && claim.status !== 'proposed')) {
      current.set(claim.claimId, claim)
    }
  }
  const claims = [...current.values()].sort((a, b) => a.claimId.localeCompare(b.claimId))
  const nodes = new Map<string, BrainNode>()
  const edges: BrainEdge[] = []
  for (const claim of claims) {
    const subject = entityNode(claim.subject)
    const claimNode: BrainNode = {
      id: `claim:${claim.claimId}`,
      kind: 'claim',
      label: claim.statement,
      status: claim.status,
      claimId: claim.claimId,
    }
    nodes.set(subject.id, subject)
    nodes.set(claimNode.id, claimNode)
    edges.push({
      id: `${subject.id}->${claimNode.id}`,
      source: subject.id,
      target: claimNode.id,
      label: claim.relation,
      claimId: claim.claimId,
      status: claim.status,
    })
    const object = objectNode(claim)
    nodes.set(object.id, object)
    edges.push({
      id: `${claimNode.id}->${object.id}`,
      source: claimNode.id,
      target: object.id,
      label: 'object',
      claimId: claim.claimId,
      status: claim.status,
    })
    claim.evidence.forEach((evidence, index) => {
      const id = `evidence:${evidence.kind}:${evidence.sha256}:${evidence.page ?? ''}`
      nodes.set(id, { id, kind: 'evidence', label: evidenceLabel(evidence), claimId: claim.claimId })
      edges.push({
        id: `${id}->${claimNode.id}:${index}`,
        source: id,
        target: claimNode.id,
        label: evidence.role,
        claimId: claim.claimId,
        status: claim.status,
      })
    })
    if (claim.sourceJobId) {
      const id = `job:${claim.sourceJobId}`
      nodes.set(id, { id, kind: 'job', label: claim.sourceJobId, claimId: claim.claimId })
      edges.push({
        id: `${id}->${claimNode.id}`,
        source: id,
        target: claimNode.id,
        label: 'produced',
        claimId: claim.claimId,
        status: claim.status,
      })
    }
  }
  return { claims, nodes: [...nodes.values()], edges, cursor }
}

/**
 * The durable projection above keeps claim/evidence/job nodes because they are
 * the complete provenance graph.  The default human view is deliberately
 * smaller: one noun/value node connected directly to another by a short verb.
 * The selected edge still points at the complete claim and its evidence.
 */
export function compactBrainGraph(graph: BrainGraph): BrainGraph {
  const nodes = new Map<string, BrainNode>()
  const edges: BrainEdge[] = []
  for (const claim of graph.claims) {
    const subject = entityNode(claim.subject)
    const object = objectNode(claim)
    if (!nodes.has(subject.id)) nodes.set(subject.id, { ...subject, claimId: claim.claimId })
    if (!nodes.has(object.id)) nodes.set(object.id, { ...object, claimId: claim.claimId })
    edges.push({
      id: `concept:${claim.claimId}`,
      source: subject.id,
      target: object.id,
      label: readableRelation(claim.relation),
      claimId: claim.claimId,
      status: claim.status,
    })
  }
  return { ...graph, nodes: [...nodes.values()], edges }
}

export function matchesBrainQuery(claim: BrainClaim, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return true
  return [
    claim.statement,
    claim.subject.label,
    claim.subject.kind,
    claim.relation,
    objectLabel(claim.object),
    claim.producer,
    claim.sourceJobId,
    ...claim.tags,
  ].some((value) => value.toLocaleLowerCase().includes(needle))
}

function parseClaim(event: KnowledgeEvent): BrainClaim | null {
  const payload = event.payload
  const candidate = object(payload.candidate)
  const subject = entity(candidate.subject)
  const relation = text(candidate.relation)
  const statement = text(candidate.statement)
  const objectValue = parseObject(candidate.object)
  if (!subject || !relation || !statement || !objectValue) return null
  return {
    claimId: text(payload.claim_id) || event.claim_id,
    versionId: text(payload.version_id) || event.version_id,
    status: text(payload.status) || event.kind.replace('knowledge.', ''),
    eventId: event.event_id,
    at: event.at,
    producer: text(candidate.producer),
    scope: stringRecord(candidate.scope),
    subject,
    relation,
    object: objectValue,
    statement,
    evidence: array(candidate.evidence).map(parseEvidence).filter((row): row is BrainEvidence => row !== null),
    checks: array(payload.checks).map(object),
    sourceJobId: text(candidate.source_job_id),
    tags: array(candidate.tags).map(text).filter(Boolean),
  }
}

function entityNode(value: BrainEntity): BrainNode {
  return { id: `entity:${value.kind}:${value.id}`, kind: 'entity', label: value.label }
}

function objectNode(claim: BrainClaim): BrainNode {
  if (claim.object.kind === 'entity') return entityNode(claim.object.entity)
  return { id: `literal:${claim.claimId}`, kind: 'literal', label: objectLabel(claim.object), claimId: claim.claimId }
}

function evidenceLabel(value: BrainEvidence): string {
  const page = value.page ? ` · p.${value.page}` : ''
  return `${value.kind} · ${value.locator}${page}`
}

export function objectLabel(value: BrainObject): string {
  if (value.kind === 'entity') return value.entity.label
  const shown = typeof value.value === 'string' ? value.value : JSON.stringify(value.value)
  return value.unit ? `${shown} ${value.unit}` : shown
}

export function conceptLabel(value: string, maximumWords = 5, maximumCharacters = 42): string {
  const clean = value.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!clean) return '—'
  const words = clean.split(' ')
  let shown = words.slice(0, maximumWords).join(' ')
  if (shown.length > maximumCharacters) shown = `${shown.slice(0, maximumCharacters - 1).trim()}…`
  else if (words.length > maximumWords) shown = `${shown}…`
  return shown
}

function readableRelation(value: string): string {
  return conceptLabel(value, 3, 26)
}

function parseObject(value: unknown): BrainObject | null {
  const row = object(value)
  if (row.kind === 'entity') {
    const parsed = entity(row.entity)
    return parsed ? { kind: 'entity', entity: parsed } : null
  }
  if (row.kind === 'literal') return { kind: 'literal', value: row.value, ...(text(row.unit) ? { unit: text(row.unit) } : {}) }
  return null
}

function parseEvidence(value: unknown): BrainEvidence | null {
  const row = object(value)
  const kind = text(row.kind)
  const role = text(row.role)
  const locator = text(row.locator)
  const sha256 = text(row.sha256)
  if (!kind || !role || !locator || !sha256) return null
  return { kind, role, locator, sha256, ...(typeof row.page === 'number' ? { page: row.page } : {}), ...(text(row.excerpt) ? { excerpt: text(row.excerpt) } : {}) }
}

function entity(value: unknown): BrainEntity | null {
  const row = object(value)
  const kind = text(row.kind)
  const id = text(row.id)
  const label = text(row.label)
  return kind && id && label ? { kind, id, label } : null
}
function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : [] }
function text(value: unknown): string { return typeof value === 'string' ? value : '' }
function stringRecord(value: unknown): Record<string, string> {
  return Object.fromEntries(Object.entries(object(value)).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
}
