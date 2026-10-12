/**
 * A contract proposal, in full, in the panel — not in the file manager.
 *
 * A draft is a handoff envelope the daemon reduced into the job result. There
 * is no file on disk, so "open" cannot mean reveal-in-Explorer the way it does
 * for a report or a run directory. It means: show the envelope verbatim, its
 * digest, the reviewer's findings and the changes it requires.
 *
 * The standing badge is the careful part. `standingFor` decides the words and
 * `contract.test.ts` holds them to never saying approved or executable —
 * `ready_for_human_review` means only that no review error or required change
 * is known, and the caveat says so on screen rather than in a comment.
 */

import { useState } from 'react'
import { promotionFrame } from '../promotion'
import type { Platform } from '../usePlatform'
import {
  findingsBySeverity,
  requiredChanges,
  standingFor,
  type Proposal,
} from '../contract'

export function ContractPanel({
  proposal,
  jobId,
  platform,
}: {
  proposal: Proposal
  jobId: string
  platform: Platform
}) {
  const standing = standingFor(proposal.reasonCode)
  const findings = findingsBySeverity(proposal.review)
  const changes = requiredChanges(proposal.review)
  const [showing, setShowing] = useState<'none' | 'draft' | 'review'>('none')
  const [requestedBy, setRequestedBy] = useState('')
  const [compiled, setCompiled] = useState<Record<string, unknown> | null>(null)
  const [approval, setApproval] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState<'preview' | 'request' | 'apply' | null>(null)
  const [message, setMessage] = useState('')

  const command = async (action: 'preview' | 'request' | 'apply') => {
    if (!platform.api) return
    setBusy(action)
    setMessage('')
    try {
      const key = action === 'apply' ? String(approval?.approval_id ?? '') : jobId
      const answer = await platform.api.command(promotionFrame(action, key, requestedBy))
      if (answer.ok !== true) throw new Error(String(answer.error ?? 'the daemon refused the request'))
      if (answer.proposal && typeof answer.proposal === 'object') {
        setCompiled(answer.proposal as Record<string, unknown>)
      }
      if (answer.approval && typeof answer.approval === 'object') {
        setApproval(answer.approval as Record<string, unknown>)
      }
      setMessage(
        action === 'preview'
          ? 'Exact generated YAML preview loaded; nothing was written.'
          : action === 'request'
            ? 'Digest-bound approval is now in the Inbox.'
            : 'Approved contract installed without overwriting an existing file.',
      )
      await platform.refresh()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'promotion command failed')
    } finally {
      setBusy(null)
    }
  }

  const approvalState = String(approval?.state ?? '')

  return (
    <div className="contract">
      <header>
        <span className={`pill ${standing.tone}`}>{standing.label}</span>
        <b>{proposal.draft?.contract_name ?? 'contract proposal'}</b>
        <span className="muted mono">{jobId}</span>
      </header>

      {/* The sentence that stops a badge from reading as a green light. */}
      <p className="caveat">{standing.caveat}</p>

      <dl className="meta">
        {proposal.draft?.execution_mode && (
          <Row label="execution mode" value={proposal.draft.execution_mode} />
        )}
        {proposal.draft?.architecture_id && (
          <Row label="architecture" value={proposal.draft.architecture_id} />
        )}
        {proposal.draftSha256 && <Row label="draft sha256" value={proposal.draftSha256} />}
        {proposal.reviewSha256 && <Row label="review sha256" value={proposal.reviewSha256} />}
        {proposal.review?.draft_sha256 && (
          <Row
            label="review covers draft"
            value={
              proposal.review.draft_sha256 === proposal.draftSha256
                ? `${proposal.review.draft_sha256.slice(0, 12)}… (matches)`
                : `${proposal.review.draft_sha256.slice(0, 12)}… (does NOT match the draft above)`
            }
          />
        )}
      </dl>

      {changes.length > 0 && (
        <section className="changes">
          <h4>Required changes ({changes.length})</h4>
          <ul>
            {changes.map((change) => (
              <li key={change}>{change}</li>
            ))}
          </ul>
        </section>
      )}

      {findings.length > 0 && (
        <section className="findings">
          <h4>Findings</h4>
          {findings.map(([severity, entries]) => (
            <div key={severity} className={`finding-group ${severity}`}>
              <b>{severity}</b>
              {entries.map((finding, index) => (
                <div key={`${finding.code ?? index}`} className="finding">
                  <span className="mono">{finding.code ?? '—'}</span>
                  <span>{finding.detail ?? ''}</span>
                </div>
              ))}
            </div>
          ))}
        </section>
      )}

      <div className="row">
        <button
          className="chip"
          onClick={() => setShowing(showing === 'draft' ? 'none' : 'draft')}
          disabled={!proposal.draft}
        >
          {showing === 'draft' ? 'hide the draft' : 'open the draft'}
        </button>
        <button
          className="chip"
          onClick={() => setShowing(showing === 'review' ? 'none' : 'review')}
          disabled={!proposal.review}
        >
          {showing === 'review' ? 'hide the review' : 'open the review'}
        </button>
        <span className="muted">
          These are envelopes, not files. Nothing here is on disk to open.
        </span>
      </div>

      {showing !== 'none' && (
        <pre className="envelope">
          {JSON.stringify(showing === 'draft' ? proposal.draft : proposal.review, null, 2)}
        </pre>
      )}

      {proposal.reasonCode === 'ready_for_human_review' && (
        <section className="promotion">
          <h4>Install as a reusable offline contract</h4>
          <p className="muted">
            Preview compiles deterministic YAML without writing. Request binds a human approval to
            the draft, review, scenario and exact YAML digest. Apply cannot succeed before that
            approval is accepted in the Inbox.
          </p>
          <div className="row">
            <button className="chip" disabled={busy !== null} onClick={() => void command('preview')}>
              {busy === 'preview' ? 'compiling…' : 'preview exact YAML'}
            </button>
            <label className="promotion-signer">
              requested by
              <input
                value={requestedBy}
                onChange={(event) => setRequestedBy(event.target.value)}
                placeholder="your name"
              />
            </label>
            <button className="chip" disabled={busy !== null || !compiled} onClick={() => void command('request')}>
              {busy === 'request' ? 'requesting…' : approval ? 'refresh approval' : 'request approval'}
            </button>
            <button
              className="chip approval-accept"
              disabled={busy !== null || approvalState !== 'granted'}
              onClick={() => void command('apply')}
            >
              {busy === 'apply' ? 'installing…' : 'install approved bytes'}
            </button>
          </div>
          {message && <p className={message.includes('refused') || message.includes('missing') ? 'warn' : 'muted'}>{message}</p>}
          {approval && (
            <dl className="meta">
              <Row label="approval" value={String(approval.approval_id ?? '')} />
              <Row label="decision" value={approvalState || 'unknown'} />
            </dl>
          )}
          {compiled && (
            <details>
              <summary>
                {String(compiled.filename ?? 'generated contract')} ·{' '}
                <span className="mono">{String(compiled.contract_sha256 ?? '').slice(0, 16)}…</span>
              </summary>
              <pre className="envelope">{JSON.stringify(compiled.contract ?? compiled, null, 2)}</pre>
            </details>
          )}
        </section>
      )}
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
