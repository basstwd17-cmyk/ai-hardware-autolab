import { useCallback, useEffect, useMemo, useState } from 'react'
import { jobIdOf } from '../../api/client'
import type { Platform } from '../usePlatform'

interface CampaignFile {
  filename: string
  sha256: string
  valid: boolean
  error?: string
  campaign?: CampaignSummary
}

interface CampaignPermission {
  id: string
  contract: string
  role: string
  tool: string
  artifact_mode: string
  artifact_kinds?: string[]
  producer_tools?: string[]
  max_approvals: number
  max_executions: number
  stage_id?: string | null
  phase?: string | null
}

interface CampaignSummary {
  active?: boolean
  hardware_enabled?: boolean
  campaign_id?: string
  schema_version?: number
  sha256?: string
  approved_by?: string
  activated_by?: string
  expires_at?: string
  max_auto_approvals?: number
  max_iterations?: number
  auto_approvals_used?: number
  auto_approvals_remaining?: number
  job_id?: string
  filename?: string
  permissions?: CampaignPermission[]
}

interface CampaignProposal {
  job_id: string
  contract: string
  contract_sha256: string
  scenario_sha256: string
  board?: { board_id?: string; revision?: string; title?: string; profile_sha256?: string } | null
  permissions: CampaignPermission[]
  blockers: string[]
  ready: boolean
  max_iterations: number
  max_auto_approvals: number
  duration_hours: number
  review_sha256: string
}

export function CampaignView({ platform }: { platform: Platform }) {
  const [files, setFiles] = useState<CampaignFile[]>([])
  const [active, setActive] = useState<CampaignSummary>({ active: false })
  const [selectedFile, setSelectedFile] = useState('')
  const [selectedJob, setSelectedJob] = useState('')
  const [approvedBy, setApprovedBy] = useState('quantum1')
  const [proposal, setProposal] = useState<CampaignProposal | null>(null)
  const [maxIterations, setMaxIterations] = useState(8)
  const [maxAutoApprovals, setMaxAutoApprovals] = useState(24)
  const [durationHours, setDurationHours] = useState(12)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [remoteLink, setRemoteLink] = useState('')

  const jobs = platform.state?.jobs ?? []
  const jobIds = useMemo(() => {
    const results = Object.entries(platform.state?.results ?? {})
      .filter(([, result]) => result.outcome === 'contract_gap' || result.reasonCode === 'contract_gap')
      .map(([jobId]) => jobId)
    return [...new Set([...results, ...jobs.map(jobIdOf)])]
  }, [jobs, platform.state?.results])
  const file = files.find((item) => item.filename === selectedFile) ?? null
  const manualPreview = file?.campaign ?? null

  const load = useCallback(async () => {
    if (!platform.api) return
    try {
      const [catalogAnswer, statusAnswer] = await Promise.all([
        platform.api.command({ type: 'campaign.catalog', commandId: crypto.randomUUID() }),
        platform.api.command({ type: 'campaign.status', commandId: crypto.randomUUID() }),
      ])
      if (catalogAnswer.ok !== true) throw new Error(String(catalogAnswer.error ?? 'campaign catalogue refused'))
      if (statusAnswer.ok !== true) throw new Error(String(statusAnswer.error ?? 'campaign status refused'))
      const catalog = asRecord(catalogAnswer)
      const nextFiles = Array.isArray(catalog.campaigns) ? catalog.campaigns as CampaignFile[] : []
      setFiles(nextFiles)
      setActive(asRecord(statusAnswer.campaign) as CampaignSummary)
      setSelectedFile((current) => current || nextFiles.find((item) => item.valid)?.filename || '')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'campaign read failed')
    }
  }, [platform.api])

  useEffect(() => {
    setSelectedJob((current) => current || jobIds[0] || '')
  }, [jobIds])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => { void load() }, 5_000)
    return () => window.clearInterval(timer)
  }, [load])

  const previewBundle = useCallback(async () => {
    if (!platform.api || !selectedJob || active.active) return
    setBusy(true); setError(''); setRemoteLink('')
    try {
      const answer = await platform.api.command({
        type: 'campaign.bundle.preview', commandId: crypto.randomUUID(), jobId: selectedJob,
        maxIterations, maxAutoApprovals, durationHours,
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'campaign proposal refused'))
      setProposal(asRecord(answer.proposal) as unknown as CampaignProposal)
    } catch (cause) {
      setProposal(null)
      setError(cause instanceof Error ? cause.message : 'campaign proposal failed')
    } finally { setBusy(false) }
  }, [platform.api, selectedJob, active.active, maxIterations, maxAutoApprovals, durationHours])

  useEffect(() => { void previewBundle() }, [previewBundle])

  const approveBundle = async () => {
    if (!platform.api || !proposal) return
    setBusy(true); setError('')
    try {
      const answer = await platform.api.command({
        type: 'campaign.bundle.approve', commandId: crypto.randomUUID(), jobId: proposal.job_id,
        approvedBy, reviewSha256: proposal.review_sha256,
        maxIterations, maxAutoApprovals, durationHours,
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'campaign approval refused'))
      setActive(asRecord(answer.campaign) as CampaignSummary)
      setRemoteLink('')
      platform.refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'campaign approval failed')
      void previewBundle()
    } finally { setBusy(false) }
  }

  const createRemoteLink = async () => {
    if (!platform.api || !proposal) return
    setBusy(true); setError('')
    try {
      const created = await platform.api.createRemoteApproval({
        jobId: proposal.job_id, ttlMinutes: 720,
        maxIterations, maxAutoApprovals, durationHours,
      })
      const link = created.urls[0] ?? new URL(created.path, window.location.origin).toString()
      setRemoteLink(link)
      await navigator.clipboard?.writeText(link).catch(() => undefined)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'remote approval link failed')
    } finally { setBusy(false) }
  }

  const activateManual = async () => {
    if (!platform.api || !file || !selectedJob) return
    setBusy(true); setError('')
    try {
      const answer = await platform.api.command({
        type: 'campaign.activate', commandId: crypto.randomUUID(), jobId: selectedJob,
        filename: file.filename, sha256: file.sha256, approvedBy,
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'campaign activation refused'))
      setActive(asRecord(answer.campaign) as CampaignSummary)
      platform.refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'campaign activation failed')
    } finally { setBusy(false) }
  }

  const deactivate = async () => {
    if (!platform.api) return
    setBusy(true); setError('')
    try {
      const answer = await platform.api.command({
        type: 'campaign.deactivate', commandId: crypto.randomUUID(), requestedBy: approvedBy,
        reason: 'stopped from the web office',
      })
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'campaign stop refused'))
      setActive(asRecord(answer.campaign) as CampaignSummary)
      platform.refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'campaign stop failed')
    } finally { setBusy(false) }
  }

  return <div className="campaign-view">
    <header className="campaign-head">
      <div><b>Overnight Campaign</b><p>검토한 보드·계약·도구 범위 안에서만 재빌드·배포·측정을 반복합니다.</p></div>
      <span className={`pill ${active.active ? 'running' : 'blocked'}`}>{active.active ? 'ACTIVE' : 'INACTIVE'}</span>
    </header>
    {error && <p className="error campaign-error">{error}</p>}
    {active.active ? <ActiveCampaign campaign={active} busy={busy} onStop={() => void deactivate()} /> : <>
      <section className="campaign-activate panel">
        <label>실행 대기 Job<select value={selectedJob} onChange={(event) => setSelectedJob(event.target.value)}>
          <option value="">선택</option>{jobIds.map((jobId) => <option key={jobId} value={jobId}>{jobId}</option>)}
        </select></label>
        <div className="campaign-limit-grid">
          <NumberField label="반복 상한" value={maxIterations} max={64} onChange={setMaxIterations} />
          <NumberField label="자동 승인 상한" value={maxAutoApprovals} max={256} onChange={setMaxAutoApprovals} />
          <NumberField label="유효 시간" value={durationHours} max={72} onChange={setDurationHours} suffix="h" />
        </div>
        <label>승인자<input value={approvedBy} onChange={(event) => setApprovedBy(event.target.value)} /></label>
        {proposal ? <ProposalScope proposal={proposal} /> : <p className="muted">완료된 하드웨어 mission을 선택하면 계약과 측정 범위를 계산합니다.</p>}
        <div className="campaign-actions">
          <button className="primary" disabled={!proposal?.ready || !approvedBy.trim() || busy} onClick={() => void approveBundle()}>{busy ? '처리 중…' : '이 범위를 승인하고 실행'}</button>
          <button disabled={!proposal?.ready || busy} onClick={() => void createRemoteLink()}>12시간 휴대폰 제어 링크</button>
        </div>
        {remoteLink && <div className="remote-link"><b>제한형 제어 링크를 복사했습니다</b><input readOnly value={remoteLink} onFocus={(event) => event.currentTarget.select()} /><small>승인 1회와 그 결과 job의 메시지·상태·중단만 허용합니다. Tailscale HTTPS origin을 설정하면 휴대폰에서 바로 열립니다.</small></div>}
      </section>
      <details className="campaign-manual panel">
        <summary>고급: 기존 Campaign YAML 직접 활성화</summary>
        <label>Campaign<select value={selectedFile} onChange={(event) => setSelectedFile(event.target.value)}>
          <option value="">선택</option>{files.map((item) => <option key={item.filename} value={item.filename} disabled={!item.valid}>{item.filename}{item.valid ? '' : ' · invalid'}</option>)}
        </select></label>
        {file && !file.valid && <p className="error">{file.error}</p>}
        {manualPreview && <CampaignScope campaign={manualPreview} digest={file?.sha256 ?? ''} />}
        <button disabled={!file?.valid || !selectedJob || !approvedBy.trim() || busy} onClick={() => void activateManual()}>선택한 YAML 활성화</button>
      </details>
    </>}
  </div>
}

function ProposalScope({ proposal }: { proposal: CampaignProposal }) {
  return <div className={`campaign-scope ${proposal.ready ? '' : 'has-blockers'}`}>
    <div className="campaign-board"><b>{proposal.board?.title ?? 'Board profile missing'}</b><small>{proposal.board ? `${proposal.board.board_id}@${proposal.board.revision}` : 'hardware execution is closed'}</small></div>
    <div className="campaign-meters"><Meter label="iterations" value={String(proposal.max_iterations)} /><Meter label="auto approvals" value={String(proposal.max_auto_approvals)} /><Meter label="duration" value={`${proposal.duration_hours}h`} /></div>
    <p className="campaign-contract"><b>{proposal.contract}</b><span>{shortDigest(proposal.contract_sha256)}</span></p>
    <PermissionList permissions={proposal.permissions} />
    {proposal.blockers.length > 0 && <ul className="campaign-blockers">{proposal.blockers.map((item) => <li key={item}>{item}</li>)}</ul>}
    <p className="mono digest" title={proposal.review_sha256}>review {proposal.review_sha256}</p>
  </div>
}

function ActiveCampaign({ campaign, busy, onStop }: { campaign: CampaignSummary; busy: boolean; onStop: () => void }) {
  return <section className="panel active-campaign">
    <div className="campaign-meters"><Meter label="iteration ceiling" value={String(campaign.max_iterations ?? '—')} /><Meter label="auto approvals" value={`${campaign.auto_approvals_remaining ?? '—'} left`} /><Meter label="expires" value={shortDate(campaign.expires_at)} /></div>
    <dl className="meta"><Row label="campaign" value={campaign.campaign_id ?? '—'} /><Row label="job" value={campaign.job_id ?? '—'} /><Row label="activated by" value={campaign.activated_by ?? '—'} /><Row label="digest" value={campaign.sha256 ?? '—'} /></dl>
    <PermissionList permissions={campaign.permissions ?? []} /><button className="danger" disabled={busy} onClick={onStop}>즉시 중단</button>
  </section>
}

function CampaignScope({ campaign, digest }: { campaign: CampaignSummary; digest: string }) {
  return <div className="campaign-scope"><div className="campaign-meters"><Meter label="iterations" value={String(campaign.max_iterations ?? '—')} /><Meter label="auto approvals" value={String(campaign.max_auto_approvals ?? '—')} /><Meter label="expires" value={shortDate(campaign.expires_at)} /></div><PermissionList permissions={campaign.permissions ?? []} /><p className="mono digest" title={digest}>{digest}</p></div>
}

function PermissionList({ permissions }: { permissions: CampaignPermission[] }) {
  return <div className="campaign-permissions">{permissions.map((permission) => <article key={permission.id}><b>{permission.role}</b><span>→ {permission.tool}</span><small>{permission.phase ? `${permission.phase} · ` : ''}{permission.contract} · {permission.artifact_mode === 'run_manifest' ? `new ${permission.artifact_kinds?.join(', ')}` : 'pinned profile'} · max {permission.max_approvals}</small></article>)}</div>
}

function NumberField({ label, value, max, suffix = '', onChange }: { label: string; value: number; max: number; suffix?: string; onChange: (value: number) => void }) {
  return <label>{label}<span><input type="number" min={1} max={max} value={value} onChange={(event) => onChange(Math.max(1, Math.min(max, Number(event.target.value) || 1)))} />{suffix}</span></label>
}
function Meter({ label, value }: { label: string; value: string }) { return <span><small>{label}</small><b>{value}</b></span> }
function Row({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd title={value}>{value}</dd></div> }
function shortDate(value?: string): string { return value ? value.replace('T', ' ').slice(0, 16) : '—' }
function shortDigest(value: string): string { return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—' }
function asRecord(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {} }
