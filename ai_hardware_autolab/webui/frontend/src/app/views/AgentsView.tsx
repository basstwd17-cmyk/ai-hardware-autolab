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
import { agentsOf, mergeConversationRows, toFeedSteps, toLiveNotes, toTurns, type Entry, type FeedStep, type Turn } from '../conversation'
import { useTranscript } from '../useTranscript'
import { handoffFacts, handoffSummary, handoffTitle, itemText } from '../handoff'
import type { Platform } from '../usePlatform'
import { TEAM_ORDER, teamDisplayName } from '../agentTeams'
import { tokenUsageByAgent, type AgentTokenUsage } from '../tokenUsage'
import { clarificationQuestion } from '../clarification'

export function AgentsView({ platform }: { platform: Platform }) {
  const jobs = platform.state?.jobs ?? []
  const [chosen, setChosen] = useState<string | null>(null)
  const [catalog, setCatalog] = useState<AgentProfile[]>([])
  const [profileName, setProfileName] = useState<string | null>(null)
  const [showAllProfiles, setShowAllProfiles] = useState(false)

  // Follow the first job until the user picks one, so the panel is never a
  // blank waiting for a click when there is exactly one thing to look at.
  const selected = chosen ?? jobs.find((job) => !isFinished(job))?.job_id ?? jobs[0]?.job_id ?? null
  const cursor = platform.connection.daemon?.cursor ?? 0
  const transcript = useTranscript(platform.api, selected, cursor)

  const liveRows = useMemo(
    () => platform.liveEvents.filter((event) => event.job_id === selected),
    [platform.liveEvents, selected],
  )
  const rows = useMemo(() => mergeConversationRows(transcript.events, liveRows, selected), [transcript.events, liveRows, selected])
  const turns = useMemo(() => toTurns(rows), [rows])
  const feed = useMemo(() => toFeedSteps(turns), [turns])
  const agents = useMemo(() => agentsOf(turns), [turns])
  const liveNotes = useMemo(() => toLiveNotes(rows), [rows])
  const tokenUsage = useMemo(() => tokenUsageByAgent(rows), [rows])
  const profiles = useMemo(
    () => new Map(catalog.map((profile) => [profile.name, profile])),
    [catalog],
  )
  const result = selected ? platform.state?.results[selected] : undefined
  const selectedJob = jobs.find((job) => job.job_id === selected)

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
          <LiveActivity notes={liveNotes} catalog={catalog} loadingHistory={transcript.loading} finished={!!selectedJob && isFinished(selectedJob)} />
          <TokenUsagePanel usage={tokenUsage} />
          <Progress
            agents={agents}
            turns={turns}
            job={transcript.job}
            result={result as Record<string, unknown> | undefined}
            jobId={selected}
            platform={platform}
          />
          <FeedbackComposer
            key={selected}
            jobId={selected}
            platform={platform}
            disabled={selectedJob ? isFinished(selectedJob) : false}
            question={clarificationQuestion(selectedJob ? waitingOf(selectedJob) : null)}
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

function TokenUsagePanel({ usage }: { usage: AgentTokenUsage[] }) {
  if (usage.length === 0) return null
  const total = usage.reduce((sum, row) => sum + row.total, 0)
  const maximum = Math.max(...usage.map((row) => row.total), 1)
  return (
    <section className="token-usage" aria-label="에이전트별 토큰 사용량">
      <header>
        <span><b>토큰 사용량</b> · 총 {tokenCount(total)}</span>
        <small>런타임 보고값 · thread별 최신 누적값</small>
      </header>
      <div className="token-usage-list">
        {usage.map((row) => (
          <details key={row.agent} className="token-agent">
            <summary>
              <span className="token-agent-name">{row.agent}</span>
              <span className="token-bar"><i style={{ width: `${Math.max(2, row.total / maximum * 100)}%` }} /></span>
              <b>{tokenCount(row.total)}</b>
              <small>{row.threads}회</small>
            </summary>
            <div className="token-breakdown">
              <span>입력 <b>{tokenCount(row.input)}</b></span>
              <span>캐시 입력 <b>{tokenCount(row.cachedInput)}</b></span>
              <span>출력 <b>{tokenCount(row.output)}</b></span>
              <span>추론 출력 <b>{tokenCount(row.reasoningOutput)}</b></span>
            </div>
          </details>
        ))}
      </div>
      <p>같은 thread의 중간 갱신은 중복 합산하지 않습니다. 토큰 이벤트를 보내지 않는 런타임 사용량은 포함되지 않습니다.</p>
    </section>
  )
}

function tokenCount(value: number): string {
  return new Intl.NumberFormat('ko-KR').format(value)
}

export function FeedbackComposer({
  jobId,
  platform,
  disabled,
  question = null,
}: {
  jobId: string
  platform: Platform
  disabled: boolean
  question?: string | null
}) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [failed, setFailed] = useState(false)

  const send = async () => {
    const message = draft.trim()
    if (!message || !platform.api || disabled || busy) return
    setBusy(true)
    setNotice('')
    setFailed(false)
    try {
      const answer = await platform.api.command({
        type: 'job.steer',
        commandId: crypto.randomUUID(),
        jobId,
        message,
        sentBy: 'local-operator',
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? '피드백이 거부되었습니다.'))
      setDraft('')
      setNotice(answer.willBeDelivered === true
        ? question ? '답변을 기록했습니다. Lead가 기존 작업과 답변을 검토해 재계획합니다.' : '원장에 기록했고, 다음 에이전트가 이 피드백을 받습니다.'
        : '원장에는 기록했지만 현재 단계에는 전달할 다음 에이전트가 없습니다.')
      platform.refresh()
    } catch (cause) {
      setFailed(true)
      setNotice(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="job-feedback" aria-label={question ? '에이전트 질문에 답변하기' : '에이전트에게 피드백 보내기'}>
      <header>
        <b>{question ? '답변 대기' : '진행 방향 피드백'}</b>
        <small>{question ? '답변을 보내면 같은 작업에서 Lead가 이어갑니다. 장비 실행 전에는 연결·권한·안전 설정을 다시 확인합니다.' : '다음 에이전트가 실행되기 전에 원장에 전달됩니다.'}</small>
      </header>
      {question && <p className="warn" role="status">{question}</p>}
      <textarea
        value={draft}
        disabled={disabled || busy}
        maxLength={4000}
        placeholder={disabled
          ? '완료된 job에는 피드백을 보낼 수 없습니다.'
          : question ? '확인한 내용을 답변해주세요. 예: LAN 연결을 복구했고 프로브 스위치는 10×입니다.'
            : '예: CH3에 연결했어. 기존 코드를 활용하고 파형 저장부터 확인해줘.'}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault()
            void send()
          }
        }}
      />
      <div className="job-feedback-actions">
        <span className={`small ${failed ? 'warn' : 'muted'}`} role="status">{notice || 'Ctrl+Enter로도 보낼 수 있습니다.'}</span>
        <button className="chip" disabled={disabled || busy || !draft.trim()} onClick={() => void send()}>
          {busy ? '기록 중…' : question ? '답변 보내기' : '피드백 보내기'}
        </button>
      </div>
    </section>
  )
}

function LiveActivity({
  notes,
  catalog,
  loadingHistory,
  finished,
}: {
  notes: ReturnType<typeof toLiveNotes>
  catalog: AgentProfile[]
  loadingHistory: boolean
  finished: boolean
}) {
  const teams = new Map(catalog.map((profile) => [profile.name, profile.team]))
  if (notes.length === 0) {
    return (
      <section className="live-activity">
        <header><b>지금 진행 중</b><span className="muted">{loadingHistory ? '이전 기록을 불러오는 중' : '새 활동 대기'}</span></header>
        <p className="muted small">실행이 시작되면 에이전트가 한 행동씩 여기에 짧게 알려줍니다.</p>
      </section>
    )
  }
  return (
    <section className="live-activity" aria-label="실시간 에이전트 진행">
      <header><b>최근 활동 · 대화</b><span className="muted">{finished ? '종료된 작업의 기록' : loadingHistory ? '기록 동기화 중' : '새 활동이 오면 이어집니다'}</span></header>
      <p className="muted small">실제 메시지와 실행 기록 요약입니다. 원문은 아래 상세 기록에서 확인할 수 있습니다.</p>
      <div className="live-notes">
        {notes.map((note) => {
          const team = teams.get(note.agent) ?? note.agent
          return (
            <article key={note.eventId} className={note.current && !finished ? 'current' : ''}>
              <span className="feed-avatar">{roleIcon(catalog.find((profile) => profile.name === note.agent)?.role ?? note.agent)}</span>
              <p><b>{teamDisplayName(team)} <small>{note.source === 'message' ? '메시지' : '활동 요약'}</small></b>{note.text}</p>
              <time>{shortTime(note.at)}</time>
            </article>
          )
        })}
      </div>
    </section>
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
  const teams = groupAgentTeams(catalog)
  const stageTeams = new Map(catalog.map((profile) => [profile.name, profile.team]))
  const activeSet = new Set(active.map((name) => stageTeams.get(name)).filter(Boolean))
  const shown = showAll ? teams : teams.filter((profile) => activeSet.has(profile.name))
  const chosen = teams.find((profile) => profile.name === selected) ?? null
  const chosenMembers = chosen
    ? catalog.filter((profile) => profile.team === chosen.name).map((profile) => profile.name)
    : []
  const actions = latestActions(turns)

  return (
    <section className="agent-strip">
      <header>
        <span><b>에이전트 팀</b> · {activeSet.size} active</span>
        <button className="link" onClick={onToggleAll}>{showAll ? '현재 팀만' : `전체 ${teams.length}`}</button>
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
              <b>{teamDisplayName(profile.name)}</b>
              <small>{roleLabel(profile.role)} · {teamAction(profile.name, catalog, actions) ?? (activeSet.has(profile.name) ? '대기 중' : '현재 작업 없음')}</small>
            </span>
            <span className="agent-badges">{permissionBadges(profile).map((badge) => <i key={badge}>{badge}</i>)}</span>
          </button>
        ))}
        {shown.length === 0 && <span className="muted small">아직 이 작업에 참여한 에이전트가 없습니다.</span>}
      </div>
      {chosen && <AgentProfileDetail profile={chosen} members={chosenMembers} onClose={() => onSelect(null)} />}
    </section>
  )
}

function AgentProfileDetail({
  profile,
  members,
  onClose,
}: {
  profile: AgentProfile
  members: string[]
  onClose: () => void
}) {
  return (
    <div className="agent-profile-detail">
      <button className="link close" onClick={onClose}>닫기</button>
      <b>{roleIcon(profile.role)} {teamDisplayName(profile.name)}</b>
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
          <Row label="내부 단계" value={members.join(' → ') || '—'} />
          <Row label="receives" value={profile.consumes.join(', ') || '—'} />
          <Row label="hands off" value={profile.emits.join(', ') || '—'} />
        </dl>
      </details>
    </div>
  )
}

const TEAM_DESCRIPTIONS: Record<string, string> = {
  lead: '시나리오를 소유하고 기존 지식과 필요한 검색을 합쳐 구현 구조와 실행 계획을 만듭니다.',
  builder: '코드 작성·빌드·프로그램·측정을 한 흐름으로 실행하고 run 증거를 남깁니다.',
  verifier: 'run 증거를 최초 요구 조건과 비교해 완료 또는 재계획을 결정합니다.',
}

function groupAgentTeams(catalog: AgentProfile[]): AgentProfile[] {
  return TEAM_ORDER.flatMap((team) => {
    const members = catalog.filter((profile) => profile.team === team)
    if (members.length === 0) return []
    const first = members[0]
    const unique = (values: string[]) => [...new Set(values)]
    return [{
      name: team,
      team,
      role: first.role,
      description: TEAM_DESCRIPTIONS[team] ?? first.description,
      mcpTools: unique(members.flatMap((profile) => profile.mcpTools)),
      localTools: unique(members.flatMap((profile) => profile.localTools)),
      skills: unique(members.flatMap((profile) => profile.skills)),
      consumes: unique(members.flatMap((profile) => profile.consumes)),
      emits: unique(members.flatMap((profile) => profile.emits)),
      touchesHardware: members.some((profile) => profile.touchesHardware),
      nativeWeb: members.some((profile) => profile.nativeWeb),
    }]
  })
}

function teamAction(
  team: string,
  catalog: AgentProfile[],
  actions: Map<string, string>,
): string | undefined {
  const members = new Set(
    catalog.filter((profile) => profile.team === team).map((profile) => profile.name),
  )
  return [...actions.entries()].reverse().find(([name]) => members.has(name))?.[1]
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

export function JobPicker({
  jobs,
  selected,
  onPick,
}: {
  jobs: JobEntry[]
  selected: string | null
  onPick: (id: string) => void
}) {
  const [query, setQuery] = useState('')
  const [showFinished, setShowFinished] = useState(false)
  const normalized = query.trim().toLowerCase()
  const matches = (job: JobEntry) => {
    if (!normalized) return true
    return jobSearchText(job).includes(normalized)
  }
  const active = jobs.filter((job) => !isFinished(job) && matches(job))
  const finished = jobs.filter((job) => isFinished(job) && matches(job))
  const selectedFinished = finished.find((job) => job.job_id === selected)
  const recentFinished = finished.slice(0, 5)
  // Keep the selected detail reachable without forcing the whole history open.
  const preview = selectedFinished && !recentFinished.includes(selectedFinished)
    ? [...recentFinished.slice(0, 4), selectedFinished] : recentFinished
  const visibleFinished = showFinished ? finished : preview

  return (
    <div className="job-picker">
      <div className="job-picker-tools">
        <input
          aria-label="작업 검색"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="job · 목표 검색"
        />
        <span className="muted small">진행 {active.length} · 완료 {finished.length}</span>
      </div>
      <JobSection label="진행 중 / 확인 필요" jobs={active} selected={selected} onPick={onPick} empty="진행 중인 작업이 없습니다." />
      {finished.length > 0 && (
        <section className="job-section completed">
          <header>
            <b>최근 완료</b>
            {finished.length > 5 && <button className="link" aria-expanded={showFinished}
              onClick={() => setShowFinished((value) => !value)}>
              {showFinished ? '접기' : `전체 ${finished.length}개`}
            </button>}
          </header>
          <div className="picker">
            {visibleFinished.map((job) => <JobButton key={job.job_id} job={job} selected={selected} onPick={onPick} />)}
          </div>
        </section>
      )}
    </div>
  )
}

function JobSection({
  label,
  jobs,
  selected,
  onPick,
  empty,
}: {
  label: string
  jobs: JobEntry[]
  selected: string | null
  onPick: (id: string) => void
  empty: string
}) {
  return (
    <section className="job-section">
      <header><b>{label}</b><span className="muted small">{jobs.length}</span></header>
      <div className="picker">
        {jobs.map((job) => <JobButton key={job.job_id} job={job} selected={selected} onPick={onPick} />)}
        {jobs.length === 0 && <span className="muted small">{empty}</span>}
      </div>
    </section>
  )
}

function JobButton({ job, selected, onPick }: { job: JobEntry; selected: string | null; onPick: (id: string) => void }) {
  const waiting = waitingOf(job)
  const state = waiting?.status ?? job.state
  const caption = jobCaption(job)
  return (
    <button
      className={`job ${job.job_id === selected ? 'on' : ''}`}
      onClick={() => onPick(job.job_id)}
      title={waiting?.detail ?? caption}
    >
      <span className={`pill ${state}`}>{clarificationQuestion(waiting) ? '답변 대기' : jobStateLabel(state)}</span>
      <span className="job-copy"><b>{caption}</b><small className="mono">{job.job_id}</small></span>
    </button>
  )
}

function isFinished(job: JobEntry): boolean {
  return job.state === 'done' || ('outcome' in job && Boolean(job.outcome))
}

function jobSearchText(job: JobEntry): string {
  const waiting = waitingOf(job)
  const row = 'objective' in job ? job : null
  return [job.job_id, row?.gate, row?.objective, job.state, 'outcome' in job ? job.outcome : '', waiting?.detail ?? '']
    .filter((value): value is string => typeof value === 'string')
    .join(' ')
    .toLowerCase()
}

function jobCaption(job: JobEntry): string {
  if ('objective' in job) return job.objective || job.gate || job.job_id
  return job.job_id
}

function jobStateLabel(state: string): string {
  return ({
    active: '실행 중', running: '진행', pending: '대기', blocked: '확인 필요', waiting_approval: '승인 대기',
    waiting_person: '확인 필요', waiting_retry: '재시도 대기', done: '완료', finished: '완료',
  } as Record<string, string>)[state] ?? state
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
    const entry = [...turn.entries].reverse().find((item) => item.kind !== 'marker' && item.kind !== 'other')
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
