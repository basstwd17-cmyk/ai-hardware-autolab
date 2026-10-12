/**
 * What a preflight answer means, and which frame it entitles the UI to send.
 *
 * Every new scenario enters the same Lead → Builder → Verifier conversation.
 * Hardware mode binds the installed board profile on the daemon.
 */

export type ExecutionScope = 'offline' | 'hardware'
export type SearchPolicy = 'refresh' | 'always'

const SCOPE_DIRECTIVE: Record<ExecutionScope, string> = {
  offline: 'Execution scope: offline; no hardware access is permitted.',
  hardware:
    'Execution scope: hardware; submitting this scenario delegates control of the devices requested below, within their electrical safety bounds. Other devices are not authorized.',
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
