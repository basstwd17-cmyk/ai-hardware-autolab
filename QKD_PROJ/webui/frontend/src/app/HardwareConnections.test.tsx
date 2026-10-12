import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ConnectionSummary } from './HardwareConnections'

it('keeps registration, verified identity and unavailable devices distinct', () => {
  const html = renderToStaticMarkup(<ConnectionSummary inventory={{
    status: 'checked', checked_at: '2026-09-28T02:00:00Z',
    setup: { board_id: 'fixture', revision: '1', title: '테스트 구성' },
    devices: [
      { kind: 'bbb', model: 'BBB', status: 'connected', run: 'connection_bbb' },
      { kind: 'scope', model: 'Scope', status: 'unavailable', detail: 'TCP timeout' },
      { kind: 'serial', model: 'New board', status: 'unsupported' },
    ],
  }} />)
  expect(html).toContain('장비 식별 확인')
  expect(html).toContain('식별 확인 실패')
  expect(html).toContain('확인 드라이버 필요')
  expect(html).toContain('TCP timeout')
  expect(html).toContain('마지막 확인')
  expect(html).toContain('connection_bbb')
})
