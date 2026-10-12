import {expect,it} from 'vitest'
import {renderToStaticMarkup} from 'react-dom/server'
import {ProjectPicker} from './ProjectPicker'
import {StartView} from './views/StartView'
import type {Platform} from './usePlatform'
it('offers a project source and build/test guidance without a board requirement',()=>{
 const html=renderToStaticMarkup(<ProjectPicker api={null} onChange={()=>{}}/>)
 for(const label of ['프로젝트 연결','소스 폴더의 절대 경로','빌드 방법','검증 방법','첫 커밋']) expect(html).toContain(label)
})
it('an offline daemon opens general development without hardware setup controls',()=>{
 const platform={connection:{daemon:{profile:'offline'}},api:null} as Platform
 const html=renderToStaticMarkup(<StartView platform={platform}/>)
 expect(html).toContain('<option value="offline" selected="">')
 expect(html).toContain('이번 작업의 프로젝트')
 expect(html).not.toContain('연결된 USB·시리얼 장치 찾기')
})
