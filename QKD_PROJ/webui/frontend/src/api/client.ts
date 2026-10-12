/**
 * Every call to the backend, typed from the schema the backend generated.
 *
 * `components['schemas'][...]` comes from `schema.d.ts`, which comes from
 * `openapi.json`, which comes from `backend/models.py`. Renaming a field in
 * Python breaks this file at build time rather than producing `undefined` in a
 * browser nobody is watching.
 */

import type { components } from './schema'

export type DaemonStatus = components['schemas']['DaemonStatus']
export type StateResponse = components['schemas']['StateResponse']
export type EventsResponse = components['schemas']['EventsResponse']
export type LedgerEvent = components['schemas']['LedgerEvent']
export type JobResult = components['schemas']['JobResult']
export type ApprovalRow = components['schemas']['ApprovalRow']
export type WaitingRow = components['schemas']['WaitingRow']
export type JobRow = components['schemas']['JobRow']
export type JobEntry = StateResponse['jobs'][number]
export type GraphSnapshot = components['schemas']['GraphSnapshot']
export type GraphLayout = components['schemas']['GraphLayout']
export type JobDetail = components['schemas']['JobDetail']
export type HandoffDetail = components['schemas']['HandoffDetail']
export type OfficeSnapshot = components['schemas']['OfficeSnapshot']
export type Occupant = components['schemas']['Occupant']
export type ArtifactOpened = components['schemas']['ArtifactOpened']
export type AgentProfile = components['schemas']['AgentProfile']
export type AgentCatalog = components['schemas']['AgentCatalog']
export type Positions = GraphLayout['positions']
export type GraphNode = components['schemas']['GraphNode']
export type GraphEdge = components['schemas']['GraphEdge']

export interface RemoteApprovalCreated {
  path: string
  urls: string[]
  expiresAt: string
  proposal: Record<string, unknown>
}

/** A job id, from either shape of `status.jobs`. */
export function jobIdOf(entry: JobEntry): string {
  return entry.job_id
}

/** The `Waiting` half of the union, when that is what this is. */
export function waitingOf(entry: JobEntry): WaitingRow | null {
  return 'detail' in entry ? (entry as WaitingRow) : null
}

export type SaveResult =
  | { ok: true; layout: GraphLayout }
  | { ok: false; current: GraphLayout }

/** FastAPI's `detail` is a string for our refusals and a list for its own. */
function detailOf(body: unknown): string | null {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => JSON.stringify(d)).join('; ')
  return null
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail)
  }
}

export class Api {
  constructor(
    private readonly token: string,
    private readonly base = '',
  ) {}

  /** The token as a query parameter, for the one place a header cannot go. */
  wsUrl(after: number): string {
    const origin = typeof window === 'undefined' ? 'http://127.0.0.1' : window.location.origin
    const url = new URL(`${this.base}/ws`, origin)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    url.searchParams.set('after', String(after))
    url.searchParams.set('token', this.token)
    return url.toString()
  }

  state(): Promise<StateResponse> {
    return this.get<StateResponse>('/api/state')
  }

  agents(): Promise<AgentCatalog> {
    return this.get<AgentCatalog>('/api/agents')
  }

  graph(): Promise<GraphSnapshot> {
    return this.get<GraphSnapshot>('/api/graph')
  }

  events(since = 0, limit = 500): Promise<EventsResponse> {
    return this.get<EventsResponse>(`/api/events?since=${since}&limit=${limit}`)
  }

  /** One job's whole history, from the ledger rather than the capped memory. */
  job(jobId: string, since = 0): Promise<JobDetail> {
    return this.get<JobDetail>(`/api/jobs/${encodeURIComponent(jobId)}?since=${since}`)
  }

  /** Durable full envelopes, fetched only when an operator opens a job. */
  handoffs(jobId: string): Promise<HandoffDetail[]> {
    return this.get<HandoffDetail[]>(`/api/jobs/${encodeURIComponent(jobId)}/handoffs`)
  }

  /**
   * Forward one operator frame to the daemon.
   *
   * A refusal comes back as `{ok: false, error}` with a 409, which is an
   * answer rather than a failure -- the daemon decided, and the reason it gave
   * is the thing worth showing.
   */
  async command(frame: Record<string, unknown>, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const answer = await fetch(`${this.base}/api/command`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(frame),
      signal,
    })
    if (answer.status === 409 || answer.ok) {
      return (await answer.json()) as Record<string, unknown>
    }
    const body = await answer.json().catch(() => null)
    throw new ApiError(answer.status, detailOf(body) ?? `${answer.status} sending ${frame.type}`)
  }

  async createRemoteApproval(request: {
    jobId: string
    ttlMinutes: number
    maxIterations: number
    maxAutoApprovals: number
    durationHours: number
  }): Promise<RemoteApprovalCreated> {
    const answer = await fetch(`${this.base}/api/remote-approvals`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    })
    if (!answer.ok) {
      const body = await answer.json().catch(() => null)
      throw new ApiError(answer.status, detailOf(body) ?? `${answer.status} creating approval link`)
    }
    return (await answer.json()) as RemoteApprovalCreated
  }

  office(): Promise<OfficeSnapshot> {
    return this.get<OfficeSnapshot>('/api/office')
  }

  /**
   * Ask the host to reveal an artifact. The browser sends a pair and gets an
   * acknowledgement -- the path never comes back, and nothing is executed.
   */
  async openArtifact(jobId: string, artifactId: string): Promise<ArtifactOpened> {
    const answer = await fetch(
      `${this.base}/api/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(
        artifactId,
      )}/open`,
      { method: 'POST', headers: { Authorization: `Bearer ${this.token}` } },
    )
    if (!answer.ok) {
      const body = await answer.json().catch(() => null)
      throw new ApiError(answer.status, detailOf(body) ?? `${answer.status} opening ${artifactId}`)
    }
    return (await answer.json()) as ArtifactOpened
  }

  layout(): Promise<GraphLayout> {
    return this.get<GraphLayout>('/api/graph/layout')
  }

  /**
   * Save, or come back with what to merge onto.
   *
   * A 409 is an *answer*, not a failure: another tab saved first and the
   * backend has handed back its layout. Throwing here would push a routine
   * two-tab case into an error path; returning it keeps the merge where it
   * belongs, in `saveMerging`.
   */
  async saveLayout(revision: number, positions: Positions): Promise<SaveResult> {
    const answer = await fetch(`${this.base}/api/graph/layout`, {
      method: 'PUT',
      headers: {
        Authorization: `Bearer ${this.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ revision, positions }),
    })
    if (answer.status === 409) {
      const body = (await answer.json()) as { current: GraphLayout }
      return { ok: false, current: body.current }
    }
    if (!answer.ok) {
      const body = await answer.json().catch(() => null)
      throw new ApiError(answer.status, detailOf(body) ?? `${answer.status} saving the layout`)
    }
    return { ok: true, layout: (await answer.json()) as GraphLayout }
  }

  private async get<T>(path: string): Promise<T> {
    const answer = await fetch(`${this.base}${path}`, {
      headers: { Authorization: `Bearer ${this.token}` },
    })
    if (!answer.ok) {
      // The backend refuses with `{ok, detail}`; anything else is a surprise
      // and is reported as its status rather than guessed at.
      const body = await answer.json().catch(() => null)
      throw new ApiError(answer.status, detailOf(body) ?? `${answer.status} from ${path}`)
    }
    return (await answer.json()) as T
  }
}
