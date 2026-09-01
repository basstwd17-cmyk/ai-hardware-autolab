/**
 * Agents, as a conversation: who did what, in order, with what came of it.
 *
 * Two things this view is careful not to claim.
 *
 * **It does not decide a job's state.** The pill next to a job comes from the
 * control plane's `status`; nothing here reads `job.done` and concludes
 * anything. Progress is stated as what the record contains — iterations
 * started, handoffs made, whether a verdict has been reduced — not as a
 * percentage nobody could check.
 *
 * **It does not pretend to have read a handoff.** The event stream carries a
 * handoff as a digest, so a handoff bubble says it handed off a Plan and shows
 * the digest. The Verdict is the exception, and only because the daemon
 * reduces it into the job result verbatim.
 *
 * A long job is read in pages until the ledger's tip, and how far it got is on
 * screen — a transcript that silently showed a prefix would be the worst kind
 * of wrong, complete-looking and missing the end.
 */

import { useEffect, useMemo, useState } from 'react'
import type { AgentProfile, HandoffDetail, JobEntry } from '../../api/client'
import { waitingOf } from '../../api/client'
import { proposalOf } from '../contract'
import { agentsOf, toFeedSteps, toTurns, type Entry, type FeedStep, type Turn } from '../conversation'
import { ContractPanel } from './ContractPanel'
import { useTranscript } from '../useTranscript'
import { handoffFacts, handoffSummary, handoffTitle, itemText } from '../handoff'
import type { Platform } from '../usePlatform'

export function AgentsView({ platform }: { platform: Platform }) {
  const jobs = platform.state?.jobs ?? []
  const [chosen, setChosen] = useState<string | null>(null)
  const [catalog, setCatalog] = useState<AgentProfile[]>([])
  const [profileName, setProfileName] = useState<string | null>(null)
  const [showAllProfiles, setShowAllProfiles] = useState(false)

  // Follow the first job until the user picks one, so the panel is never a
  // blank waiting for a click when there is exactly one thing to look at.
  const selected = chosen ?? jobs[0]?.job_id ?? null
  const cursor = platform.connection.daemon?.cursor ?? 0
  const transcript = useTranscript(platform.api, selected, cursor)

  const turns = useMemo(() => toTurns(transcript.events), [transcript.events])
  const feed = useMemo(() => toFeedSteps(turns), [turns])
  const agents = useMemo(() => agentsOf(turns), [turns])
  const profiles = useMemo(
    () => new Map(catalog.map((profile) => [profile.name, profile])),
    [catalog],
  )
  const result = selected ? platform.state?.results[selected] : undefined

  useEffect(() => {
    let alive = true
    platform.api?.agents()
      .then((answer) => { if (alive) setCatalog(answer.agents) })
      .catch(() => { if (alive) setCatalog([]) })
    return () => { alive = false }
  }, [platform.api])

  return (
    <div className="agents">
      <JobPicker jobs={jobs} selected={selected} onPick={setChosen} />

      {!selected && <p className="muted pad">No jobs on this ledger yet.</p>}
      {transcript.error && <p className="error pad">{transcript.error}</p>}

      {selected && (
        <>
          <AgentStrip
            catalog={catalog}
            active={agents}
            turns={turns}
            selected={profileName}
            onSelect={setProfileName}
            showAll={showAllProfiles}
            onToggleAll={() => setShowAllProfiles((value) => !value)}
          />
          <Progress
            agents={agents}
            turns={turns}
            job={transcript.job}
            result={result as Record<string, unknown> | undefined}
            jobId={selected}
            platform={platform}
          />
          <HandoffResults api={platform.api} jobId={selected} cursor={transcript.total} />
          <div className="thread">
            {feed.map((step, index) => (
              <FeedCard
                key={`${step.agent}-${step.at}-${index}`}
                step={step}
                profile={profiles.get(step.agent)}
                current={index === feed.length - 1 && !step.closedBy}
              />
            ))}
            <Tail transcript={transcript} turns={turns.length} />
          </div>
        </>
      )}
    </div>
  )
}

/**
 * Read the actual durable envelopes, separately from the lightweight event
 * transcript.  The transcript proves when a handoff occurred; this panel
 * shows the body the next agent received.
 */
function HandoffResults({
  api,
  jobId,
  cursor,
}: {
  api: Platform['api']
  jobId: string
  cursor: number
}) {
  const [handoffs, setHandoffs] = useState<HandoffDetail[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    if (!api) {
      setHandoffs([])
      return () => { alive = false }
    }
    api.handoffs(jobId)
      .then((rows) => {
        if (!alive) return
        setHandoffs(rows)
        setError(null)
      })
      .catch((cause) => {
        if (!alive) return
        setHandoffs([])
        setError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => { alive = false }
  }, [api, jobId, cursor])

  const selected = handoffs.find((handoff) => handoff.handoff_id === selectedId) ?? handoffs.at(-1)
  if (error) return <p className="warn pad">결과 원문을 읽지 못했습니다: {error}</p>
  if (handoffs.length === 0) return null

  return (
    <section className="handoff-results" aria-label="작업 결과">
      <header>
        <span><b>생성된 결과</b> · {handoffs.length}개</span>
        <small>원장에 저장된 에이전트 전달본</small>
      </header>
      <div className="handoff-rail" role="tablist" aria-label="결과 단계">
        {handoffs.map((handoff) => (
          <button
            key={handoff.handoff_id}
            role="tab"
            aria-selected={selected?.handoff_id === handoff.handoff_id}
            className={selected?.handoff_id === handoff.handoff_id ? 'on' : ''}
            onClick={() => setSelectedId(handoff.handoff_id)}
          >
            {handoffTitle(handoff.kind)}
          </button>
        ))}
      </div>
      {selected && <HandoffBody handoff={selected} />}
    </section>
  )
}

function HandoffBody({ handoff }: { handoff: HandoffDetail }) {
  const summary = handoffSummary(handoff)
  const facts = handoffFacts(handoff)
  const arrays = Object.entries(handoff.payload as Record<string, unknown>)
    .filter(([, value]) => Array.isArray(value))
    .slice(0, 4) as Array<[string, unknown[]]>

  return (
    <div className="handoff-body" role="tabpanel">
      <div className="handoff-heading">
        <span className="feed-avatar">{roleIcon(handoff.agent)}</span>
        <span><b>{handoffTitle(handoff.kind)}</b><small>{handoff.agent} · iteration {handoff.iteration}</small></span>
        <code title={handoff.sha256}>#{handoff.sha256.slice(0, 12)}</code>
      </div>
      {summary && <p className="handoff-summary">{summary}</p>}
      {facts.length > 0 && <div className="handoff-facts">
        {facts.map(([label, count]) => <span key={label}>{label} <b>{count}</b></span>)}
      </div>}
      {arrays.map(([key, values]) => {
        const readable = values.map(itemText).filter(Boolean).slice(0, 6)
        if (readable.length === 0) return null
        return (
          <section className="handoff-list" key={key}>
            <b>{key}</b>
            <ul>{readable.map((value, index) => <li key={`${value}-${index}`}>{value}</li>)}</ul>
          </section>
        )
      })}
      <details className="handoff-raw">
        <summary>전달 원문(JSON) · 검증 해시 {handoff.sha256.slice(0, 12)}…</summary>
        <pre>{JSON.stringify(handoff.payload, null, 2)}</pre>
      </details>
    </div>
  )
}

function AgentStrip({
  catalog,
  active,
  turns,
  selected,
  onSelect,
  showAll,
  onToggleAll,
}: {
  catalog: AgentProfile[]
  active: string[]
  turns: Turn[]
  selected: string | null
  onSelect: (name: string | null) => void
  showAll: boolean
  onToggleAll: () => void
}) {
  const activeSet = new Set(active)
  const shown = showAll ? catalog : catalog.filter((profile) => activeSet.has(profile.name))
  const chosen = catalog.find((profile) => profile.name === selected) ?? null
  const actions = latestActions(turns)

  return (
    <section className="agent-strip">
      <header>
        <span><b>에이전트</b> · {active.length} active</span>
        <button className="link" onClick={onToggleAll}>{showAll ? '현재 팀만' : `전체 ${catalog.length}`}</button>
      </header>
      <div className="agent-cards">
        {shown.map((profile) => (
          <button
            key={profile.name}
            className={`agent-card ${selected === profile.name ? 'on' : ''} ${activeSet.has(profile.name) ? 'active' : ''}`}
            onClick={() => onSelect(selected === profile.name ? null : profile.name)}
          >
            <span className={`agent-avatar role-${profile.role}`}>{roleIcon(profile.role)}</span>
            <span className="agent-card-copy">
              <b>{profile.name}</b>
              <small>{roleLabel(profile.role)} · {actions.get(profile.name) ?? (activeSet.has(profile.name) ? '대기 중' : '현재 작업 없음')}</small>
            </span>
            <span className="agent-badges">{permissionBadges(profile).map((badge) => <i key={badge}>{badge}</i>)}</span>
          </button>
        ))}
        {shown.length === 0 && <span className="muted small">아직 이 작업에 참여한 에이전트가 없습니다.</span>}
      </div>
      {chosen && <AgentProfileDetail profile={chosen} onClose={() => onSelect(null)} />}
    </section>
  )
}

function AgentProfileDetail({ profile, onClose }: { profile: AgentProfile; onClose: () => void }) {
  return (
    <div className="agent-profile-detail">
      <button className="link close" onClick={onClose}>닫기</button>
      <b>{roleIcon(profile.role)} {profile.name}</b>
      <p>{profile.description}</p>
      <div className="profile-facts">
        <span><small>역할</small>{roleLabel(profile.role)}</span>
        <span><small>하드웨어</small>{profile.touchesHardware ? '접근 가능' : '접근 없음'}</span>
        <span><small>도구</small>{profile.mcpTools.length + profile.localTools.length}</span>
        <span><small>스킬</small>{profile.skills.length || '—'}</span>
      </div>
      <details>
        <summary>권한과 전달 형식 보기</summary>
        <dl className="meta">
          <Row label="MCP" value={profile.mcpTools.join(', ') || '—'} />
          <Row label="local" value={profile.localTools.join(', ') || '—'} />
          <Row label="skills" value={profile.skills.join(', ') || '—'} />
          <Row label="receives" value={profile.consumes.join(', ') || '—'} />
          <Row label="hands off" value={profile.emits.join(', ') || '—'} />
        </dl>
      </details>
    </div>
  )
}

function Tail({
  transcript,
  turns,
}: {
  transcript: ReturnType<typeof useTranscript>
  turns: number
}) {
  if (transcript.loading) {
    return (
      <p className="muted">
        reading… {transcript.loaded} row{transcript.loaded === 1 ? '' : 's'}
      </p>
    )
  }
  if (transcript.truncated) {
    return (
      <p className="warn">
        Stopped at {transcript.loaded} rows, short of the ledger&rsquo;s {transcript.total}. This is
        a page ceiling, not the end of the job.{' '}
        <button className="link" onClick={transcript.reload}>
          read again
        </button>
      </p>
    )
  }
  if (turns === 0) {
    return <p className="muted">This job has no rows that read as a turn yet.</p>
  }
  return (
    <p className="muted small">
      {transcript.loaded} row{transcript.loaded === 1 ? '' : 's'}, to the ledger&rsquo;s tip
    </p>
  )
}

function JobPicker({
  jobs,
  selected,
  onPick,
}: {
  jobs: JobEntry[]
  selected: string | null
  onPick: (id: string) => void
}) {
  return (
    <div className="picker">
      {jobs.map((job) => {
        const waiting = waitingOf(job)
        const state = waiting?.status ?? job.state
        return (
          <button
            key={job.job_id}
            className={`job ${job.job_id === selected ? 'on' : ''}`}
            onClick={() => onPick(job.job_id)}
            title={waiting?.detail ?? ''}
          >
            <span className={`pill ${state}`}>{state}</span>
            <span className="mono">{job.job_id}</span>
          </button>
        )
      })}
    </div>
  )
}

function Progress({
  agents,
  turns,
  job,
  result,
  jobId,
  platform,
}: {
  agents: string[]
  turns: Turn[]
  job: Record<string, unknown> | null
  result: Record<string, unknown> | undefined
  jobId: string
  platform: Platform
}) {
  const row = ((job?.job ?? {}) as Record<string, unknown>) ?? {}
  const handoffs = turns.filter((t) => t.closedBy).length
  const interrupted = job?.interrupted === true
  const state = String(row.state ?? '')
  const terminal = state === 'done' || Boolean(row.outcome)

  return (
    <section className="progress">
      <div className="progress-row">
        <span className="muted">agents</span>
        <span>
          {agents.length === 0
            ? '—'
            : agents.map((name) => (
                <b key={name} className="who" data-agent={name}>
                  {name}
                </b>
              ))}
        </span>
      </div>
      <div className="progress-row">
        <span className="muted">iteration</span>
        <span>
          {String(row.iteration ?? '—')} · {handoffs} handoff{handoffs === 1 ? '' : 's'} ·{' '}
          {turns.length} turn{turns.length === 1 ? '' : 's'}
        </span>
      </div>
      {interrupted && (
        <p className="warn">
          A previous process left this job running. Whether its step reached the board is not
          known from the ledger, and nothing here assumes it did not.
        </p>
      )}
      {result && <ResultCard result={result} jobId={jobId} platform={platform} />}
      {!terminal && <CancelJobButton jobId={jobId} platform={platform} />}
    </section>
  )
}

function CancelJobButton({ jobId, platform }: { jobId: string; platform: Platform }) {
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  const cancel = async () => {
    if (!armed) {
      setArmed(true)
      setMessage('다시 누르면 이 job을 종료하고 남아 있는 승인을 철회합니다.')
      return
    }
    if (!platform.api) return
    setBusy(true)
    setMessage('')
    try {
      const answer = await platform.api.command({
        type: 'job.cancel',
        commandId: crypto.randomUUID(),
        jobId,
        cancelledBy: 'local-operator',
        reason: 'cancelled from local web UI',
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'the daemon refused cancellation'))
      setMessage('중단을 기록했습니다. 다음 단계는 실행되지 않습니다.')
      platform.refresh()
    } catch (cause) {
      setArmed(false)
      setMessage(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="job-cancel">
      <button className={`danger ${armed ? 'armed' : ''}`} onClick={() => void cancel()} disabled={busy}>
        {busy ? '중단 기록 중…' : armed ? '정말 이 작업을 중단' : '작업 중단'}
      </button>
      <small>{message || '취소는 원장에 남고, 아직 실행되지 않은 단계와 남은 승인을 철회합니다.'}</small>
    </div>
  )
}

function ResultCard({
  result,
  jobId,
  platform,
}: {
  result: Record<string, unknown>
  jobId: string
  platform: Platform
}) {
  // A contract proposal is a different kind of result: its draft is an
  // envelope, not a file, so it gets the panel rather than the reveal button.
  const proposal = proposalOf(result)
  if (proposal) return <ContractPanel proposal={proposal} jobId={jobId} platform={platform} />

  const artifacts = Array.isArray(result.artifacts)
    ? (result.artifacts as Array<{ artifactId: string; kind?: string; label?: string }>)
    : []

  return (
    <div className="verdict">
      <b>{String(result.outcome ?? 'result')}</b>
      <p>{String(result.summary ?? '')}</p>
      {typeof result.verdictSha256 === 'string' && (
        <span className="muted mono">verdict {result.verdictSha256.slice(0, 12)}…</span>
      )}
      {artifacts.length > 0 && (
        <div className="artifacts">
          {artifacts.map((artifact) => (
            <ArtifactButton
              key={artifact.artifactId}
              jobId={jobId}
              artifact={artifact}
              platform={platform}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ArtifactButton({
  jobId,
  artifact,
  platform,
}: {
  jobId: string
  artifact: { artifactId: string; kind?: string; label?: string }
  platform: Platform
}) {
  const [state, setState] = useState<'idle' | 'opening' | 'shown'>('idle')
  const [failed, setFailed] = useState<string | null>(null)

  const click = async () => {
    if (!platform.api) return
    setState('opening')
    setFailed(null)
    try {
      await platform.api.openArtifact(jobId, artifact.artifactId)
      setState('shown')
    } catch (exc) {
      // Every refusal is a check the host re-ran at click time: deleted since
      // registration, moved out of the project root, or an id two
      // registrations disagreed about. Showing the reason beats a dead button.
      setFailed(exc instanceof Error ? exc.message : String(exc))
      setState('idle')
    }
  }

  return (
    <span className="artifact">
      <button className="chip" onClick={click} disabled={state === 'opening'} title="reveal in the file manager — this never opens or runs the file">
        {artifact.label || artifact.artifactId}
        {artifact.kind && <b>{artifact.kind}</b>}
      </button>
      {state === 'shown' && <em className="muted"> shown</em>}
      {failed && <em className="warn"> {failed}</em>}
    </span>
  )
}

function FeedCard({
  step,
  profile,
  current,
}: {
  step: FeedStep
  profile?: AgentProfile
  current: boolean
}) {
  return (
    <article className={`feed-step ${current ? 'current' : ''}`}>
      <header>
        <span className={`feed-avatar role-${profile?.role ?? 'system'}`}>{roleIcon(profile?.role ?? step.agent)}</span>
        <span className="feed-author">
          <b className="who" data-agent={step.agent}>{step.agent}</b>
          <small>{profile ? roleLabel(profile.role) : step.agent === 'human' ? '사용자' : '플랫폼'}</small>
        </span>
        {step.iteration !== null && <span className="muted">iteration {step.iteration}</span>}
        <span className={`feed-state ${current ? 'live' : ''}`}>{current ? '진행 중' : step.title}</span>
      </header>
      <h3>{step.title}</h3>
      {step.summary && <p className="feed-summary">{compact(step.summary, 220)}</p>}
      {step.chips.length > 0 && <div className="feed-chips">
        {step.chips.map((chip) => <span key={chip}>{chip}</span>)}
      </div>}
      <details className="feed-audit">
        <summary>세부 로그 {step.entries.length}개{step.closedBy ? ` · ${step.closedBy.kind} 다이제스트` : ''}</summary>
        <div className="feed-audit-body">
          {step.entries.map((entry) => <EntryBlock key={entry.eventId} entry={entry} />)}
        </div>
      </details>
    </article>
  )
}

function EntryBlock({ entry }: { entry: Entry }) {
  const [open, setOpen] = useState(false)
  const body = compact(entry.body, 190)
  const previewFacts = entry.facts.filter(([key]) => key !== 'output').slice(0, 3)
  const hasDetails = entry.body.length > 190 || entry.facts.length > previewFacts.length || entry.truncated

  return (
    <div className={`entry feed-entry ${entry.kind}`}>
      <div className="entry-head">
        <span className="entry-title">{entryIcon(entry.kind)} {entry.title}</span>
        <span className="muted mono">{shortTime(entry.at)} · #{entry.eventId}</span>
      </div>
      {entry.kind === 'handoff' && !entry.body && (
        <p className="feed-summary muted">원장에 다이제스트로 전달했습니다.</p>
      )}
      {body && <p className="feed-summary">{body}</p>}
      {previewFacts.length > 0 && (
        <div className="feed-chips">
          {previewFacts.map(([key, value]) => <span key={key}><b>{key}</b> {compact(value, 54)}</span>)}
        </div>
      )}
      {entry.truncated && (
        <p className="warn small">8 KB 이후의 런타임 내용은 원장에서 잘렸습니다.</p>
      )}
      {hasDetails && <button className="link feed-more" onClick={() => setOpen(!open)}>{open ? '접기' : '자세히'}</button>}
      {open && (
        <div className="feed-detail">
          {entry.body && <pre className={entry.kind === 'command' ? 'shell' : undefined}>{entry.body}</pre>}
          {entry.facts.length > 0 && (
            <dl className="meta">
              {entry.facts.map(([key, value]) => (
                <div key={key}><dt>{key}</dt><dd title={value}>{value}</dd></div>
              ))}
            </dl>
          )}
        </div>
      )}
    </div>
  )
}

function latestActions(turns: Turn[]): Map<string, string> {
  const result = new Map<string, string>()
  for (const turn of turns) {
    const entry = [...turn.entries].reverse().find((item) => item.kind !== 'marker')
    if (entry) result.set(turn.agent, entry.title)
  }
  return result
}

function permissionBadges(profile: AgentProfile): string[] {
  const badges = [profile.role.toUpperCase()]
  if (profile.nativeWeb) badges.push('WEB')
  if (profile.localTools.some((tool) => tool === 'Write' || tool === 'Edit')) badges.push('WRITE')
  if (profile.touchesHardware) badges.push('HW')
  if (profile.skills.length > 0) badges.push('SKILL')
  return badges.slice(0, 3)
}

function roleLabel(role: string): string {
  return ({
    discover: '계획', research: '조사', plan: '설계', build: '빌드', operate: '보드 실행',
    measure: '측정', verify: '검증', report: '보고',
  } as Record<string, string>)[role] ?? role
}

function roleIcon(role: string): string {
  return ({
    discover: '🧭', research: '🔎', plan: '🧠', build: '🛠', operate: '⚡',
    measure: '📟', verify: '✓', report: '📝', human: '👤', platform: '◆',
  } as Record<string, string>)[role] ?? '●'
}

function entryIcon(kind: Entry['kind']): string {
  return ({
    brief: '✦', message: '💬', command: '⌘', files: '▤', reasoning: '◇', subagent: '↳',
    collab: '↔', handoff: '→', approval: '◈', marker: '·', other: '•',
  } as Record<Entry['kind'], string>)[kind]
}

function compact(value: string, limit: number): string {
  const clean = value.replace(/\s+/g, ' ').trim()
  return clean.length > limit ? `${clean.slice(0, limit - 1)}…` : clean
}

function shortTime(value: string): string {
  const match = value.match(/T(\d\d:\d\d:\d\d)/)
  return match?.[1] ?? ''
}

function Row({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd title={value}>{value}</dd></div>
}
