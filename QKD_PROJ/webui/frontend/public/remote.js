const $ = (id) => document.getElementById(id)
let token = sessionStorage.getItem('rtase.operatorToken') || ''
let deviceId = sessionStorage.getItem('rtase.deviceId') || 'lab-pc'
let selectedJob = ''
let currentJobs = []
let proposal = null
let timer = null

$('device').value = deviceId
$('token').value = token

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `${response.status} ${path}`)
  return body
}

async function submit(kind, payload) {
  const answer = await api('/api/relay/commands', {
    method: 'POST',
    body: JSON.stringify({ commandId: crypto.randomUUID(), deviceId, kind, payload, expiresInSeconds: 600 }),
  })
  toast(`${kind} 명령을 기록했습니다.`)
  await refresh()
  return answer
}

async function connect() {
  token = $('token').value.trim()
  deviceId = $('device').value.trim()
  if (!token || !deviceId) return toast('장치 이름과 token을 입력하세요.')
  try {
    await api(`/api/relay/state?deviceId=${encodeURIComponent(deviceId)}`)
    sessionStorage.setItem('rtase.operatorToken', token)
    sessionStorage.setItem('rtase.deviceId', deviceId)
    $('login').hidden = true
    $('workspace').hidden = false
    await refresh()
    clearInterval(timer)
    timer = setInterval(refresh, 3000)
  } catch (error) { toast(error.message) }
}

async function refresh() {
  try {
    const state = await api(`/api/relay/state?deviceId=${encodeURIComponent(deviceId)}`)
    $('online').textContent = state.online ? 'ONLINE' : 'OFFLINE'
    $('online').className = `pill ${state.online ? 'online' : 'offline'}`
    $('cursor').textContent = `cursor ${state.cursor || 0}`
    renderJobs(state.snapshot?.jobs || [])
    renderApprovals(state.snapshot?.inbox || [])
    renderCommands(state.commands || [])
    const preview = (state.commands || []).find((row) => row.kind === 'campaign.bundle.preview' && row.state === 'completed' && row.ok && row.result?.proposal)
    if (preview && (!proposal || proposal.review_sha256 !== preview.result.proposal.review_sha256)) {
      proposal = preview.result.proposal
      renderProposal()
    }
  } catch (error) {
    $('online').textContent = 'ERROR'
    $('online').className = 'pill offline'
    toast(error.message)
  }
}

function renderApprovals(approvals) {
  const root = $('approvals')
  root.replaceChildren()
  $('approval-count').textContent = `${approvals.length} pending`
  if (!approvals.length) {
    const empty = document.createElement('p')
    empty.className = 'muted'
    empty.textContent = '대기 중인 승인이 없습니다.'
    root.append(empty)
    return
  }
  for (const row of approvals) {
    const item = document.createElement('article')
    item.className = 'approval'

    const heading = document.createElement('div')
    heading.className = 'approval-heading'
    const title = document.createElement('b')
    title.textContent = row.contract || '계약 미지정'
    const step = document.createElement('span')
    step.textContent = row.step_id || 'step 미지정'
    heading.append(title, step)

    const scope = document.createElement('p')
    scope.textContent = row.scope || '승인 범위 설명 없음'
    const metadata = document.createElement('small')
    const artifactCount = Array.isArray(row.artifacts) ? row.artifacts.length : 0
    metadata.textContent = `${row.approval_id} · artifact ${artifactCount}개${row.expires_at ? ` · ${row.expires_at} 만료` : ''}`

    const note = document.createElement('textarea')
    note.className = 'approval-note'
    note.maxLength = 1000
    note.placeholder = '승인 또는 거절 이유, 다음 방향(선택)'

    const actions = document.createElement('div')
    actions.className = 'actions'
    const accept = document.createElement('button')
    accept.className = 'primary'
    accept.textContent = '승인'
    const decline = document.createElement('button')
    decline.className = 'danger'
    decline.textContent = '거절'

    const decide = async (decision) => {
      try {
        accept.disabled = true
        decline.disabled = true
        await submit('approval.decide', {
          approvalId: row.approval_id,
          decision,
          signedBy: actor(),
          note: note.value.trim(),
        })
      } catch (error) {
        accept.disabled = false
        decline.disabled = false
        toast(error.message)
      }
    }
    accept.addEventListener('click', () => decide('accepted'))
    decline.addEventListener('click', () => decide('declined'))
    actions.append(accept, decline)
    item.append(heading, scope, metadata, note, actions)
    root.append(item)
  }
}

function renderJobs(jobs) {
  currentJobs = jobs
  if (selectedJob && !jobs.some((row) => row.job_id === selectedJob)) selectedJob = ''
  if (!selectedJob && jobs.length) {
    const preferred = jobs.find((row) => row.state !== 'done') || jobs[0]
    selectedJob = preferred.job_id
  }
  $('selected-job').textContent = selectedJob || '선택 없음'
  sessionStorage.setItem('rtase.selectedJob', selectedJob)
  setActionState()
  const root = $('jobs')
  root.replaceChildren()
  if (!jobs.length) {
    const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = '아직 동기화된 job이 없습니다.'; root.append(empty)
    renderSelectedSummary()
    return
  }
  for (const row of jobs) {
    const button = document.createElement('button')
    button.className = `job ${row.job_id === selectedJob ? 'selected' : ''}`
    const title = document.createElement('b'); title.textContent = row.objective || row.job_id
    const id = document.createElement('small'); id.textContent = row.job_id
    const state = document.createElement('span'); state.className = 'state'; state.textContent = row.outcome || row.state || row.reason || 'unknown'
    button.append(title, id, state)
    button.addEventListener('click', () => { selectedJob = row.job_id; renderJobs(jobs) })
    root.append(button)
  }
  renderSelectedSummary()
}

function renderSelectedSummary() {
  const root = $('job-summary')
  root.replaceChildren()
  const job = currentJobs.find((row) => row.job_id === selectedJob)
  if (!job) {
    const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'Job을 선택하면 검증 결과와 다음 행동을 표시합니다.'; root.append(empty)
    return
  }

  const top = document.createElement('div')
  top.className = 'summary-top'
  top.append(chip(job.scheduler_status || job.state || 'unknown', statusTone(job)))
  if (job.phase) top.append(chip(job.phase, 'phase'))
  if (Number.isInteger(job.iteration)) top.append(chip(`iteration ${job.iteration}`, 'plain'))
  root.append(top)

  const objective = document.createElement('h3')
  objective.className = 'summary-objective'
  objective.textContent = job.objective || job.gate || job.job_id
  root.append(objective)

  const meta = document.createElement('p')
  meta.className = 'summary-meta'
  meta.textContent = [job.current_agent ? `담당 ${job.current_agent}` : '', job.updated_at ? `업데이트 ${formatTime(job.updated_at)}` : '', job.gate || ''].filter(Boolean).join(' · ')
  root.append(meta)

  if (job.detail) addSummaryBlock(root, '현재 정지 이유', job.detail, 'warning')

  if (job.progress?.total) {
    const progress = document.createElement('section')
    progress.className = 'summary-block'
    const label = document.createElement('div'); label.className = 'progress-label'; label.textContent = `보고된 단계 ${job.progress.reported}/${job.progress.total}`
    const track = document.createElement('div'); track.className = 'progress-track'
    const fill = document.createElement('span'); fill.style.width = `${Math.min(100, Math.round(job.progress.reported / job.progress.total * 100))}%`
    track.append(fill); progress.append(label, track); root.append(progress)
  }

  if (job.result) {
    const result = document.createElement('section')
    result.className = `summary-block result-block ${job.result.outcome || ''}`
    const title = document.createElement('h3')
    title.textContent = job.result.provenance === 'verdict' ? '검증 결과' : '최근 전달 결과'
    const summary = document.createElement('p'); summary.textContent = job.result.summary || job.result.outcome || '결과 설명 없음'
    result.append(title, summary)
    const counts = job.result.counts
    if (counts?.total) {
      const countRow = document.createElement('div'); countRow.className = 'count-row'
      countRow.append(chip(`충족 ${counts.met || 0}`, 'good'), chip(`미충족 ${counts.unmet || 0}`, counts.unmet ? 'bad' : 'plain'), chip(`증거 없음 ${counts.evidence_missing || 0}`, counts.evidence_missing ? 'warn' : 'plain'), chip(`전체 ${counts.total}`, 'plain'))
      result.append(countRow)
    }
    if (Array.isArray(job.result.conditions) && job.result.conditions.length) {
      const conditions = document.createElement('ul'); conditions.className = 'condition-list'
      for (const row of job.result.conditions) {
        const item = document.createElement('li')
        const state = document.createElement('span'); state.className = row.met ? 'condition-met' : 'condition-open'; state.textContent = row.met ? '✓' : '○'
        const field = document.createElement('b'); field.textContent = row.field || 'unnamed condition'
        const detail = document.createElement('small'); detail.textContent = `${row.status || 'unknown'}${row.evidence?.length ? ` · 근거 ${row.evidence.length}` : ''}`
        item.append(state, field, detail); conditions.append(item)
      }
      result.append(conditions)
    }
    if (job.result.evidence_runs?.length) {
      const evidence = document.createElement('small'); evidence.className = 'evidence-line'; evidence.textContent = `run 근거: ${job.result.evidence_runs.join(', ')}`; result.append(evidence)
    }
    root.append(result)
  }

  if (job.next_action) {
    const parts = [job.next_action.agent, job.next_action.tool, job.next_action.step_id].filter(Boolean).join(' · ')
    addSummaryBlock(root, '다음 행동', `${job.next_action.label || job.next_action.kind}${parts ? `\n${parts}` : ''}${job.next_action.retry_at ? `\n재시도 ${formatTime(job.next_action.retry_at)}` : ''}`, job.next_action.kind === 'human' || job.next_action.kind === 'approval' ? 'warning' : 'next')
  }

  if (job.feedback?.pending_count) {
    const latest = job.feedback.latest
    addSummaryBlock(root, `전달 대기 피드백 ${job.feedback.pending_count}개`, latest ? `${latest.by || 'human'}: ${latest.message || ''}` : '다음 에이전트에게 전달될 메시지가 있습니다.', 'feedback')
  }

  if (Array.isArray(job.recent_activity) && job.recent_activity.length) {
    const section = document.createElement('section'); section.className = 'summary-block'
    const title = document.createElement('h3'); title.textContent = '최근 에이전트 활동'
    const timeline = document.createElement('ol'); timeline.className = 'activity-list'
    for (const row of [...job.recent_activity].reverse()) {
      const item = document.createElement('li')
      const head = document.createElement('b'); head.textContent = `${row.agent || 'platform'} · ${row.kind}`
      const text = document.createElement('span'); text.textContent = row.summary || '구조화된 handoff 기록'
      const at = document.createElement('small'); at.textContent = `${formatTime(row.at)} · ${String(row.sha256 || '').slice(0, 12)}…`
      item.append(head, text, at); timeline.append(item)
    }
    section.append(title, timeline); root.append(section)
  }
}

function addSummaryBlock(root, titleText, bodyText, tone = '') {
  const block = document.createElement('section'); block.className = `summary-block ${tone}`
  const title = document.createElement('h3'); title.textContent = titleText
  const body = document.createElement('p'); body.textContent = bodyText
  block.append(title, body); root.append(block)
}

function chip(text, tone = 'plain') {
  const node = document.createElement('span'); node.className = `summary-chip ${tone}`; node.textContent = text; return node
}

function statusTone(job) {
  if (job.outcome === 'satisfied') return 'good'
  if (job.scheduler_status === 'waiting_person' || job.scheduler_status === 'waiting_approval') return 'warn'
  if (job.state === 'done' && job.outcome !== 'satisfied') return 'bad'
  return 'plain'
}

function formatTime(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function renderCommands(commands) {
  const root = $('commands'); root.replaceChildren()
  for (const row of commands.slice(0, 12)) {
    const item = document.createElement('div')
    item.className = `command ${row.state} ${row.ok === true ? 'ok' : row.ok === false ? 'bad' : ''}`
    const title = document.createElement('b'); title.textContent = `${row.kind} · ${row.state}`
    const detail = document.createElement('small'); detail.textContent = row.error || summarize(row.result)
    item.append(title, detail); root.append(item)
  }
}

function renderProposal() {
  $('proposal').hidden = !proposal
  if (!proposal) return
  const board = proposal.board?.title || proposal.board?.board_id || 'board missing'
  const permissions = (proposal.permissions || []).map((row) => `${row.role} → ${row.tool}`).join('\n')
  $('proposal-text').textContent = `${board}\n${proposal.contract}\niterations ${proposal.max_iterations} · approvals ${proposal.max_auto_approvals} · ${proposal.duration_hours}h\n${permissions}\nreview ${proposal.review_sha256}`
}

function setActionState() {
  for (const id of ['steer', 'campaign-preview', 'revise', 'cancel']) $(id).disabled = !selectedJob
}

function actor() { const value = $('actor').value.trim(); if (!value) throw new Error('보내는 사람을 입력하세요.'); return value }
function message(required = true) { const value = $('message').value.trim(); if (required && !value) throw new Error('피드백이나 진행 방향을 입력하세요.'); return value }

$('connect').addEventListener('click', connect)
$('disconnect').addEventListener('click', () => { sessionStorage.clear(); location.reload() })
$('start-mission').addEventListener('click', async () => {
  try { await submit('mission.start', { objective: $('objective').value.trim(), scenario: $('scenario').value.trim(), searchPolicy: $('search-policy').value }) } catch (error) { toast(error.message) }
})
$('steer').addEventListener('click', async () => { try { await submit('job.steer', { jobId: selectedJob, sentBy: actor(), message: message() }); $('message').value = '' } catch (error) { toast(error.message) } })
$('campaign-preview').addEventListener('click', async () => { try { proposal = null; renderProposal(); await submit('campaign.bundle.preview', { jobId: selectedJob, maxIterations: 8, maxAutoApprovals: 24, durationHours: 12 }) } catch (error) { toast(error.message) } })
$('approve').addEventListener('click', async () => { try { if (!proposal?.ready) throw new Error((proposal?.blockers || ['Campaign이 준비되지 않았습니다.']).join('; ')); await submit('campaign.bundle.approve', { jobId: selectedJob, approvedBy: actor(), reviewSha256: proposal.review_sha256, maxIterations: proposal.max_iterations, maxAutoApprovals: proposal.max_auto_approvals, durationHours: proposal.duration_hours, comment: message(false) }); proposal = null; renderProposal() } catch (error) { toast(error.message) } })
$('revise').addEventListener('click', async () => { try { await submit('job.mission.revise', { jobId: selectedJob, requestedBy: actor(), message: message() }) } catch (error) { toast(error.message) } })
$('cancel').addEventListener('click', async () => { try { if (confirm('선택한 job을 중단할까요?')) await submit('job.cancel', { jobId: selectedJob, cancelledBy: actor(), reason: message(false) || 'stopped from Cloudflare Pages' }) } catch (error) { toast(error.message) } })

function summarize(value) {
  if (!value) return ''
  if (value.error) return String(value.error)
  if (value.job?.job_id) return `${value.job.job_id} · ${value.job.state || 'created'}`
  if (value.campaign?.active) return 'campaign active'
  if (value.proposal?.review_sha256) return `review ${value.proposal.review_sha256.slice(0, 12)}…`
  return 'completed'
}

let toastTimer
function toast(text) { const node = $('notice'); node.textContent = text; node.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.remove('show'), 3500) }

selectedJob = sessionStorage.getItem('rtase.selectedJob') || ''
if (token) connect()
