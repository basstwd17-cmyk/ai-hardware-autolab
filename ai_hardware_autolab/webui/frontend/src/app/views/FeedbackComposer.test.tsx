import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { FeedbackComposer } from './AgentsView'
import type { Platform } from '../usePlatform'

it('renders the question and existing feedback channel as an answer composer', () => {
  const html = renderToStaticMarkup(<FeedbackComposer jobId="job_test" platform={{} as Platform}
    disabled={false} question="스코프 연결과 실제 프로브 배율을 알려주세요." />)
  expect(html).toContain('답변 대기')
  expect(html).toContain('스코프 연결과 실제 프로브 배율을 알려주세요.')
  expect(html).toContain('답변 보내기')
  expect(html).toContain('장비 실행 전에는 연결·권한·안전 설정을 다시 확인합니다.')
  expect(html).not.toContain('Campaign 승인을 사용해')
})

it('keeps ordinary feedback distinct from answering a question', () => {
  const html = renderToStaticMarkup(<FeedbackComposer jobId="job_test" platform={{} as Platform} disabled={false} />)
  expect(html).toContain('진행 방향 피드백')
  expect(html).not.toContain('답변 대기')
})
