/**
 * New missions have three agents. Historical stage names collapse into the
 * same three seats so old jobs remain readable without reviving the old flow.
 */

export const TEAM_ORDER = [
  'lead',
  'builder',
  'verifier',
] as const

const TEAM_BY_STAGE: Record<string, string> = {
  'lead-agent': 'lead',
  'mission-architect': 'lead',
  'gate-architect': 'lead',
  researcher: 'lead',
  'system-architect': 'lead',
  'change-analyst': 'lead',
  'architecture-selector': 'lead',
  'contract-drafter': 'lead',
  'contract-reviewer': 'lead',
  builder: 'builder',
  'design-builder': 'builder',
  'firmware-builder': 'builder',
  'board-operator': 'builder',
  measurement: 'builder',
  verifier: 'verifier',
  reporter: 'verifier',
}

const SHORT_LABEL: Record<string, string> = {
  lead: 'LEAD',
  builder: 'BUILD',
  verifier: 'VERIFY',
}

const DISPLAY_NAME: Record<string, string> = {
  lead: 'Lead Agent',
  builder: 'Builder',
  verifier: 'Verifier',
}

export function teamForStage(stage: string): string {
  return TEAM_BY_STAGE[stage] ?? stage
}

export function shortTeamLabel(team: string): string {
  return SHORT_LABEL[team] ?? team.slice(0, 10).toUpperCase()
}

export function teamDisplayName(team: string): string {
  return DISPLAY_NAME[team] ?? team
}
