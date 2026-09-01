/**
 * What a preflight answer means, and which frame it entitles the UI to send.
 *
 * `ScenarioPreflight` is deterministic: it decides whether a *reviewed*
 * contract covers a scenario. Two of its answers matter here and they are not
 * degrees of the same thing:
 *
 * Both **`ready`** and **`contract_gap`** enter `job.mission`. A ready contract
 * is a baseline, not a reason to skip checking whether an improvement or
 * repair needs refreshed sources and a contract amendment.
 *
 * Anything else — `ambiguous`, a status this build has never seen — offers no
 * button. Guessing between two doors is worse than saying which answer came
 * back and letting a person choose.
 *
 * The rule that keeps the two apart is one line and it is tested: **`ready`
 * never sends `job.discover`, and `contract_gap` never sends `job.start`.**
 * The first would start a job against no contract; the second would spend a
 * discovery run on a question already answered.
 */

export type PreflightStatus = 'ready' | 'contract_gap' | string
export type ExecutionScope = 'offline' | 'hardware'
export type SearchPolicy = 'refresh' | 'always'

const SCOPE_DIRECTIVE: Record<ExecutionScope, string> = {
  offline: 'Execution scope: offline; no hardware access is permitted.',
  hardware:
    'Execution scope: hardware; on board measurement or programming may be requested, subject to approval.',
}

/**
 * Make the execution boundary part of the scenario that is hashed and stored.
 *
 * This is deliberately not a side-channel field.  `scenario.preflight` and
 * `job.start` both bind their decisions to the scenario digest, so a mode
 * selected only in browser state could disappear between the two clicks.
 */
export function scenarioWithScope(scenario: string, scope: ExecutionScope): string {
  return `${SCOPE_DIRECTIVE[scope]}\n\n${scenario.trim()}`
}

export interface Preflight {
  status: PreflightStatus
  reason_code?: string
  detail?: string
  scenario?: string
  objective?: string
  scenario_sha256?: string
  selected_contract?: { name?: string; sha256?: string } | null
  execution_mode?: string
  approval_required?: boolean
  candidates?: Array<{ name?: string; status?: string; score?: number }>
}

export type Action =
  | { kind: 'mission'; label: string; why: string; frame: Record<string, unknown> }
  | { kind: 'none'; label: null; why: string }

/**
 * The frame this preflight entitles the UI to send, built from the preflight's
 * own values.
 *
 * `commandId` is the caller's, so a retry is the caller's decision rather than
 * something this function quietly makes unique.
 */
export function actionFor(
  preflight: Preflight | null,
  commandId: string,
  searchPolicy: SearchPolicy = 'refresh',
): Action {
  if (!preflight) {
    return { kind: 'none', label: null, why: 'no preflight has been run for this scenario yet' }
  }

  const scenario = preflight.scenario ?? ''
  const objective = preflight.objective ?? scenario
  const scenarioSha256 = preflight.scenario_sha256 ?? ''

  if (preflight.status === 'ready' || preflight.status === 'contract_gap') {
    if (!scenarioSha256) {
      return {
        kind: 'none',
        label: null,
        why: 'the preflight returned no scenario digest, and the mission is keyed on it',
      }
    }
    const derivingFromOverlap =
      preflight.status === 'contract_gap' && preflight.reason_code === 'ambiguous_contract'
    return {
      kind: 'mission',
      label:
        preflight.status === 'ready'
          ? '분석·계획 시작'
          : derivingFromOverlap
            ? '파생 계약·계획 시작'
            : '조사·계획 시작',
      why:
        preflight.status === 'ready'
          ? `${preflight.selected_contract?.name ?? 'the reviewed contract'} becomes the baseline; changed and unknown parts are refreshed`
          : derivingFromOverlap
            ? `${preflight.candidates?.length ?? 0} overlapping contracts become parents; the mission derives one scenario-specific contract automatically`
            : 'no reviewed contract covers this scenario yet — the mission researches and derives one',
      frame: {
        type: 'job.mission',
        commandId,
        objective,
        scenario,
        scenarioSha256,
        searchPolicy,
      },
    }
  }

  return {
    kind: 'none',
    label: null,
    why: `preflight answered ${preflight.status}${
      preflight.detail ? ` — ${preflight.detail}` : ''
    }`,
  }
}

/** How the answer should read on screen. `contract_gap` is not a failure. */
export function toneFor(status: PreflightStatus): 'good' | 'open' | 'unclear' {
  if (status === 'ready') return 'good'
  if (status === 'contract_gap') return 'open'
  return 'unclear'
}
