import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { StartView } from './StartView'
import type { Platform } from '../usePlatform'

it('shows hardware connection checks and the current automatic scenario flow', () => {
  const html = renderToStaticMarkup(<StartView platform={{} as Platform} />)
  expect(html).toContain('작업 시작')
  expect(html).toContain('Lead')
  expect(html).not.toContain('기존 계약')
  expect(html).toContain('선택한 구성에서 제공하는 연결 확인·통신·업로드·측정 도구를 자동 실행')
  expect(html).toContain('이번 작업의 보드·장비 구성')
  expect(html).toContain('연결된 USB·시리얼 장치 찾기')
  expect(html).toContain('장비 연결 확인')
  expect(html).toContain('<option value="hardware" selected="">')
  expect(html).toContain('같은 작업에서 질문하고 답변 후 이어갑니다.')
  expect(html).toContain('필요한 제조사 자료는 웹 검색으로 조사')
  expect(html).not.toContain('Inbox에서 한 번 확인')
  expect(html).not.toContain('in the words a contract would be written against')
})
