/**
 * What is waiting on a person, and what this host refused to pass on.
 *
 * The button records a person's explicit decision; it never computes one.
 * The daemon still re-checks the grant and artifact hashes at use time.
 */

import { useState } from 'react'
import type { ApprovalRow } from '../../api/client'
import { approvalDecisionFrame, type HumanDecision } from '../approval'
import type { Platform } from '../usePlatform'

export function InboxView({ platform }: { platform: Platform }) {
  const state = platform.state
  if (!state) return <p className="muted pad">reading…</p>

  return (
    <div className="pad">
      <h2>
        Waiting on a person <b>{state.inbox.length}</b>
      </h2>
      {state.inbox.length === 0 && <p className="muted">Nothing is blocked on an approval.</p>}
      {state.inbox.map((approval) => (
        <ApprovalCard key={approval.approval_id} approval={approval} platform={platform} />
      ))}

      {state.warnings.length > 0 && (
        <div className="warnings">
          <h3>Refused by this host</h3>
          {state.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      )}
    </div>
  )
}

function ApprovalCard({ approval, platform }: { approval: ApprovalRow; platform: Platform }) {
  const [signedBy, setSignedBy] = useState('')
  const [note, setNote] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState<HumanDecision | null>(null)
  const [message, setMessage] = useState('')

  const decide = async (decision: HumanDecision) => {
    if (!platform.api) return
    setMessage('')
    try {
      if (!reviewed) throw new Error('Confirm that you reviewed the scope and artifact digests.')
      const frame = approvalDecisionFrame(approval.approval_id, decision, signedBy, note)
      setBusy(decision)
      const answer = await platform.api.command(frame)
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'the daemon refused the decision'))
      setMessage(decision === 'accepted' ? 'Decision recorded as accepted.' : 'Decision recorded as declined.')
      await platform.refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'decision failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <span className="mono">{approval.approval_id}</span>
        <span className={`pill ${approval.state}`}>{approval.state}</span>
      </div>
      <dl className="meta">
        <Row label="job" value={approval.job_id} />
        <Row label="hardware" value={approval.contract} />
        <Row label="step" value={approval.step_id} />
        <Row label="scope" value={approval.scope} />
        <Row label="executions" value={String(approval.max_executions)} />
        {approval.expires_at && <Row label="expires" value={approval.expires_at} />}
        {approval.bundle_sha256 && (
          <Row label="bundle" value={`${approval.bundle_sha256.slice(0, 16)}…`} />
        )}
      </dl>
      <p className="muted">프로그램·펌웨어 배포처럼 장비 상태가 바뀌는 이 호출에만 적용됩니다. 조회·진단·측정은 설치된 보드 프로필 범위에서 자동 실행됩니다. 작업 취소 시 미사용 승인은 철회됩니다.</p>
      {Object.keys(approval.artifacts ?? {}).length > 0 && (
        <dl className="meta">
          {Object.entries(approval.artifacts ?? {}).map(([name, digest]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd title={digest}>{digest.slice(0, 16)}…</dd>
            </div>
          ))}
        </dl>
      )}
      <label className="approval-review">
        <input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />
        위 장비 작업과 파일 해시를 확인했습니다.
      </label>
      <div className="approval-fields">
        <label>
          signed by
          <input value={signedBy} onChange={(event) => setSignedBy(event.target.value)} placeholder="your name" />
        </label>
        <label>
          note (optional)
          <input value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </div>
      <div className="row">
        <button className="chip approval-accept" disabled={busy !== null} onClick={() => void decide('accepted')}>
          {busy === 'accepted' ? '기록 중…' : `이 단계 ${approval.max_executions}회 승인`}
        </button>
        <button className="chip approval-decline" disabled={busy !== null} onClick={() => void decide('declined')}>
          {busy === 'declined' ? 'recording…' : 'decline'}
        </button>
        {message && <span className={message.includes('recorded') ? 'muted' : 'warn'}>{message}</span>}
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  )
}
