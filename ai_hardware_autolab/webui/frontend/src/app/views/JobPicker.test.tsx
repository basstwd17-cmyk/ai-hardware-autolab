import {expect, it} from 'vitest'
import {renderToStaticMarkup} from 'react-dom/server'
import {JobPicker} from './AgentsView'
import type {JobEntry} from '../../api/client'

const jobs = Array.from({length:12}, (_, i) => ({job_id: 'job_'+i, state:'done', objective:'Finished '+i, outcome:'satisfied'})) as JobEntry[]
const render = (items: JobEntry[], selected: string) => renderToStaticMarkup(<JobPicker jobs={items} selected={selected} onPick={()=>{}} />)
it('starts collapsed even when a completed job is selected by default',()=>{
  const html = render(jobs,'job_0')
  expect(html.match(/class="job /g)).toHaveLength(5)
  expect(html).toContain('aria-expanded="false"')
  expect(html).not.toContain('job_11')
})
it('keeps an older selected job within a five-item collapsed preview',()=>{
  const html = render(jobs,'job_11')
  expect(html.match(/class="job /g)).toHaveLength(5)
  expect(html).toContain('job_11')
  expect(html).not.toContain('job_5')
  expect(jobs).toHaveLength(12)
})
it('does not offer a toggle when all results already fit the preview',()=>{
  const html = render(jobs.slice(0,3),'job_0')
  expect(html.match(/class="job /g)).toHaveLength(3)
  expect(html).not.toContain('aria-expanded')
})
