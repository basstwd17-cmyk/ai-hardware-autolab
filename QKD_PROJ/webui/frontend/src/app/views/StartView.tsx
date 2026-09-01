/**
 * Ask the preflight, then open one durable mission.
 *
 * A reviewed contract is carried in as the baseline; a contract gap is
 * carried in as an explicit unknown. Both go through the same first planning
 * agent so improvements and repairs cannot silently skip source refresh.
 *
 * **`contract_gap` is not styled as a failure.** It is the deterministic
 * answer "no reviewed contract covers this yet", and the button under it
 * starts a job that goes and finds out. Red text there would tell an operator
 * the platform is broken when it has just told them something true.
 *
 * **A click is not a result.** The button reports what the daemon answered —
 * `command.result` — and the job appears in the other panels because the
 * ledger says it exists, not because this page thinks it started one.
 */

import { useState } from 'react'
import {
  actionFor,
  scenarioWithScope,
  toneFor,
  type Action,
  type ExecutionScope,
  type Preflight,
  type SearchPolicy,
} from '../discovery'
import type { Platform } from '../usePlatform'

export function StartView({ platform }: { platform: Platform }) {
  const [scenario, setScenario] = useState('')
  const [scope, setScope] = useState<ExecutionScope>('offline')
  const [searchPolicy, setSearchPolicy] = useState<SearchPolicy>('refresh')
  const [preflight, setPreflight] = useState<Preflight | null>(null)
  const [busy, setBusy] = useState<'preflight' | 'send' | null>(null)
  const [answer, setAnswer] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [commandId] = useState(() => `webui-${Math.random().toString(36).slice(2, 10)}`)

  const action = actionFor(
    preflight,
    `${commandId}-${preflight?.status ?? 'none'}`,
    searchPolicy,
  )

  const runPreflight = async () => {
    if (!platform.api || !scenario.trim()) return
    setBusy('preflight')
    setFailed(null)
    setAnswer(null)
    try {
      const result = await platform.api.command({
        type: 'scenario.preflight',
        commandId: `${commandId}-preflight`,
        scenario: scenarioWithScope(scenario, scope),
      })
      setPreflight((result.preflight as Preflight) ?? null)
      if (!result.ok) setFailed(String(result.error ?? 'the daemon refused the preflight'))
    } catch (exc) {
      setFailed(String(exc))
    } finally {
      setBusy(null)
    }
  }

  const send = async () => {
    if (!platform.api || action.kind === 'none') return
    setBusy('send')
    setFailed(null)
    try {
      const result = await platform.api.command(action.frame)
      // What the daemon said, not what this page hoped.
      setAnswer(
        result.ok
          ? `accepted: ${JSON.stringify(result.job ?? result, null, 2)}`
          : `refused: ${String(result.error ?? 'no reason given')}`,
      )
      platform.refresh()
    } catch (exc) {
      setFailed(String(exc))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="pad start">
      <h2>Start something</h2>
      <p className="muted">
        The preflight finds a reviewed baseline when one exists. Every accepted scenario then
        enters mission planning, source refresh, change analysis and contract review.
      </p>

      <label className="scope-field">
        <span>Execution scope</span>
        <select
          value={scope}
          onChange={(event) => {
            setScope(event.target.value as ExecutionScope)
            setPreflight(null)
            setAnswer(null)
          }}
        >
          <option value="offline">Offline — hardware access forbidden</option>
          <option value="hardware">Hardware — approval still required</option>
        </select>
      </label>
      <p className="muted scope-note">
        The selected boundary becomes part of the signed scenario text. Hardware mode does not
        grant approval; it only allows preflight to route the request honestly.
      </p>

      <label className="scope-field">
        <span>Research policy</span>
        <select
          value={searchPolicy}
          onChange={(event) => {
            setSearchPolicy(event.target.value as SearchPolicy)
            setPreflight(null)
            setAnswer(null)
          }}
        >
          <option value="refresh">Refresh — reuse known facts, research changed or unknown parts</option>
          <option value="always">Always search — refresh every mission question</option>
        </select>
      </label>
      <p className="muted scope-note">
        The mission architect selects materials and agents; this policy is preserved in its durable plan.
      </p>

      <textarea
        className="scenario"
        rows={4}
        placeholder="Describe the scenario, in the words a contract would be written against."
        value={scenario}
        onChange={(event) => {
          setScenario(event.target.value)
          setPreflight(null)
          setAnswer(null)
        }}
      />
      <div className="row">
        <button className="chip" onClick={runPreflight} disabled={busy !== null || !scenario.trim()}>
          {busy === 'preflight' ? 'asking…' : 'Run preflight'}
        </button>
      </div>

      {failed && <p className="error">{failed}</p>}
      {preflight && <PreflightPanel preflight={preflight} action={action} onSend={send} busy={busy} />}
      {answer && <pre className="answer">{answer}</pre>}
    </div>
  )
}

function PreflightPanel({
  preflight,
  action,
  onSend,
  busy,
}: {
  preflight: Preflight
  action: Action
  onSend: () => void
  busy: 'preflight' | 'send' | null
}) {
  const tone = toneFor(preflight.status)

  return (
    <section className={`preflight ${tone}`}>
      <div className="preflight-head">
        <span className={`pill ${tone}`}>{preflight.status}</span>
        {preflight.reason_code && <span className="muted mono">{preflight.reason_code}</span>}
      </div>
      {preflight.detail && <p>{preflight.detail}</p>}

      {preflight.status === 'contract_gap' && (
        <p className="muted">
          {preflight.reason_code === 'ambiguous_contract'
            ? 'This is not a failure: the matching contracts will be compared as parents and the mission will derive one scenario-specific contract automatically.'
            : 'This is not a failure: the mission will research the gap and derive a scenario-specific contract automatically.'}
        </p>
      )}

      <dl className="meta">
        {preflight.selected_contract?.name && (
          <Row label="contract" value={preflight.selected_contract.name} />
        )}
        {preflight.execution_mode && <Row label="mode" value={preflight.execution_mode} />}
        {preflight.approval_required !== undefined && (
          <Row label="approval" value={String(preflight.approval_required)} />
        )}
        {preflight.scenario_sha256 && (
          <Row label="scenario sha256" value={preflight.scenario_sha256} />
        )}
      </dl>

      {(preflight.candidates?.length ?? 0) > 0 && (
        <details>
          <summary className="muted">
            {preflight.candidates?.length} contract(s) were considered
          </summary>
          <dl className="meta">
            {preflight.candidates?.map((candidate) => (
              <Row
                key={candidate.name}
                label={candidate.name ?? '?'}
                value={`${candidate.status ?? ''} · ${candidate.score ?? ''}`}
              />
            ))}
          </dl>
        </details>
      )}

      <div className="row">
        {action.kind === 'none' ? (
          <span className="muted">{action.why}</span>
        ) : (
          <>
            <button
              className={`chip ${action.kind}`}
              onClick={onSend}
              disabled={busy !== null}
              title={action.why}
            >
              {busy === 'send' ? 'sending…' : action.label}
            </button>
            <span className="muted">{action.why}</span>
          </>
        )}
      </div>
      {action.kind !== 'none' && (
        <details>
          <summary className="muted">the frame this will send</summary>
          <pre>{JSON.stringify(action.frame, null, 2)}</pre>
        </details>
      )}
    </section>
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
