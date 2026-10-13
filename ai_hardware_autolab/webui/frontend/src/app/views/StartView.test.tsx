import {expect, it} from 'vitest'
import {renderToStaticMarkup} from 'react-dom/server'
import {StartView} from './StartView'
import type {Platform} from '../usePlatform'

it('offers project development without bundled board setup', () => {
  const html = renderToStaticMarkup(<StartView platform={{} as Platform} />)
  expect(html).toContain('프로젝트')
  expect(html).toContain('격리된 작업공간')
  expect(html).not.toContain('Hardware —')
  expect(html).not.toContain('장비 구성')
})
