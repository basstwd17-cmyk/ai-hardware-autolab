import type { HandoffDetail } from '../api/client'

export const handoffTitle = (kind: string): string => ({
  Brief: '요청',
  MissionPlan: '실행 계획',
  Plan: '실행 계획',
  SourcePacket: '수집 자료',
  ChangeAnalysis: '차이 분석',
  ArchitectureDecision: '구조 결정',
  ContractDraft: '계약 초안',
  ContractReview: '계약 검토',
  StepResult: '실행 결과',
  Verdict: '판정',
  Report: '최종 보고',
}[kind] ?? kind)

const clean = (value: unknown): string => typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''

/** The one short sentence that heads an envelope; never fabricate one. */
export function handoffSummary(handoff: HandoffDetail): string {
  const body = handoff.payload as Record<string, unknown>
  for (const key of ['summary', 'objective', 'recommendation', 'decision', 'conclusion']) {
    const value = clean(body[key])
    if (value) return value
  }
  return ''
}

/** A compact index of real arrays, so a packet can be scanned before read. */
export function handoffFacts(handoff: HandoffDetail): Array<[string, number]> {
  const body = handoff.payload as Record<string, unknown>
  const labels: Record<string, string> = {
    sources: '자료', claims: '근거', findings: '발견', questions: '확인점',
    decisions: '결정', steps: '단계', changes: '변경', conditions: '조건',
  }
  return Object.entries(labels).flatMap(([key, label]) =>
    Array.isArray(body[key]) ? [[label, body[key].length] as [string, number]] : [],
  )
}

export function itemText(value: unknown): string {
  if (typeof value === 'string') return clean(value)
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const item = value as Record<string, unknown>
  for (const key of ['title', 'name', 'summary', 'statement', 'question', 'finding', 'decision', 'id']) {
    const text = clean(item[key])
    if (text) return text
  }
  return ''
}
