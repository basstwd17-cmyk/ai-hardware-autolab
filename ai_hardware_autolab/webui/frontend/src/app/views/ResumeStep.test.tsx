import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ResumeStep, recoveryKind } from './AgentsView'
import type { Platform } from '../usePlatform'

it('distinguishes checked pre-device resume from hardware replay and requires a note', () => {
  const html = renderToStaticMarkup(<ResumeStep jobId="job_test" platform={{} as Platform} reason="platform_deployment: changed source" />)
  expect(html).toContain('원인 해결 후 재개')
  expect(html).toContain('장비 상태가 불명확하면 재실행하지 않습니다')
  expect(html).toContain('재개를 위한 해결 내용')
  expect(html).toContain('disabled=""')
})

it('shows planning retry instead of the driver instructions for legacy JSON failures', () => {
  const reason = "runtime_failure: lead-agent could not run while planning: lead-agent returned an invalid handoff JSON string: Expecting ',' delimiter"
  expect(recoveryKind(reason)).toBe('plan')
  const html = renderToStaticMarkup(<ResumeStep jobId="j" platform={{} as Platform} reason={reason} />)
  expect(html).toContain('계획 응답 형식 오류')
  expect(html).toContain('기존 자료로 계획 다시 작성')
  expect(html).not.toContain('적용·재시작을 확인')
})

it('does not offer driver resume for unknown runtime failures', () => {
  const html = renderToStaticMarkup(<ResumeStep jobId="j" platform={{} as Platform} reason="runtime_failure: device timeout" />)
  expect(html).toContain('중단 원인 확인 필요')
  expect(html).not.toContain('<button')
})

it('explains when the planning retry is already spent', () => {
  const html = renderToStaticMarkup(<ResumeStep jobId="j" platform={{} as Platform} reason="plan_format: syntax" retryUsed />)
  expect(html).toContain('이미 사용했습니다')
  expect(html).toContain('disabled=""')
})
