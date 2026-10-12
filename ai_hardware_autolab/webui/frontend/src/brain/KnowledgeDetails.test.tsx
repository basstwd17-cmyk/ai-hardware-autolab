import {expect,it} from 'vitest'
import {renderToStaticMarkup} from 'react-dom/server'
import {KnowledgeDetails, KnowledgeText, safeSourceUrl} from './KnowledgeDetails'
import type {BrainClaim} from './model'

it('shows concrete notes, files, research findings and full nested results',()=>{
  const claim={claimId:'claim-1',versionId:'v1',status:'proposed',eventId:1,evidence:[],checks:[],sourceJobId:'job',tags:[],subject:{label:'Parser implementation',kind:'software',id:'parser'},scope:{project_id:'parser'},relation:'work_note',producer:'builder',at:'now',statement:'Parser created',object:{kind:'literal',value:{note:'Decision: keep quoted fields.\n\nChecks: 17 tests passed.',project:{name:'Analysis tool'},artifacts:[{path:'src/parser.py',sha256:'a'.repeat(64)}],sources:[{title:'CSV specification',url:'https://example.com/spec',finding:'Quoted commas stay within one field.'}],observation:{measurements:{rows:42},ok:true},workspace:'C:/Research/workspaces/job'}}} as BrainClaim
  const html=renderToStaticMarkup(<KnowledgeDetails claim={claim}/>)
  for(const text of ['17 tests passed','src/parser.py','CSV specification','Quoted commas','42','Analysis tool','원본 기록 전체']) expect(html).toContain(text)
  expect(html).toContain('href="https://example.com/spec"')
})
it('preserves code blocks and escapes source HTML',()=>{
  const fence=String.fromCharCode(96).repeat(3)
  const html=renderToStaticMarkup(<KnowledgeText text={'# Implementation\n\n'+fence+'python\nprint(5)\n'+fence+'\n\n<script>alert(1)</script>'}/>)
  expect(html).toContain('<pre><code>print(5)')
  expect(html).not.toContain('<script>')
  expect(html).toContain('&lt;script&gt;')
})
it('links only http and https sources',()=>{
  expect(safeSourceUrl('javascript:alert(1)')).toBeNull()
  expect(safeSourceUrl('file:///C:/secret')).toBeNull()
  expect(safeSourceUrl('https://example.com/docs')).toBe('https://example.com/docs')
})
