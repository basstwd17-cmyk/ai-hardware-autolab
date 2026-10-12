import type {BrainClaim} from './model'

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
export function safeSourceUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {const url=new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : null} catch {return null}
}

/** React text nodes preserve full stored content; no source HTML is executed. */
export function KnowledgeText({text}: {text: string}) {
  const fence=String.fromCharCode(96).repeat(3)
  const pattern=new RegExp('('+fence+'[^\\n]*\\n[\\s\\S]*?'+fence+')','g')
  return <div className="knowledge-content">{text.split(pattern).filter(Boolean).map((part,i)=> {
    if (part.startsWith(fence)) {
      const newline=part.indexOf('\n')
      return <pre key={i}><code>{part.slice(newline+1, -3)}</code></pre>
    }
    return part.split(/\n\s*\n/).filter(Boolean).map((paragraph,j)=>{
      if (/^#{1,6}\s/.test(paragraph)) return <h4 key={i+':'+j}>{paragraph.replace(/^#{1,6}\s/, '')}</h4>
      return <p key={i+':'+j}>{paragraph}</p>
    })
  })}</div>
}
function StoredValue({value}: {value:unknown}) {
  return typeof value==='string' ? <KnowledgeText text={value}/> : <pre className="knowledge-json">{JSON.stringify(value,null,2)}</pre>
}
export function KnowledgeDetails({claim}: {claim: BrainClaim}) {
  const data=claim.object.kind==='literal' ? record(claim.object.value) : {}
  const project=record(data.project)
  const sources=Array.isArray(data.sources) ? data.sources.map(record) : []
  if (data.url) sources.unshift({url:data.url,title:claim.subject.label,finding:data.finding})
  const artifacts=Array.isArray(data.artifacts) ? data.artifacts : []
  const runs=Array.isArray(data.runs) ? data.runs : []
  const observation=record(data.observation)
  const fullText=typeof data.note==='string' ? data.note : claim.statement
  const extra=Object.fromEntries(Object.entries(data).filter(([key])=>!['note','summary','project','artifacts','runs','sources','url','finding','observation','job_id','event_id','at','workspace','status'].includes(key)))
  return <article className="knowledge-details" aria-label="지식 기록 상세">
    <h3>{claim.subject.label}</h3>
    <dl className="knowledge-meta">
      <div><dt>프로젝트</dt><dd>{String(project.name || claim.scope.project_id || '공통 지식')}</dd></div>
      <div><dt>기록 종류</dt><dd>{({work_note:'개발 메모',research_source:'조사 자료',device_observation:'장비 반환 기록'} as Record<string,string>)[claim.relation] || claim.relation}</dd></div>
      <div><dt>작성</dt><dd>{claim.producer} · {claim.at}</dd></div>
      {data.status !== undefined && <div><dt>응답 상태</dt><dd>{String(data.status)}</dd></div>}
    </dl>
    <h3>내용</h3><KnowledgeText text={fullText}/>
    {typeof data.summary==='string' && data.summary!==fullText && <details className="note-details"><summary>이 작업 단계의 요약</summary><KnowledgeText text={data.summary}/></details>}
    {sources.length>0 && <section><h3>참고 문서와 조사 내용</h3>{sources.map((source,i)=>{
      const url=safeSourceUrl(source.url)
      return <article className="knowledge-source" key={i}>
        {url ? <a href={url} target="_blank" rel="noreferrer">{String(source.title || url)}</a> : <strong>{String(source.title || source.url || '참고 자료')}</strong>}
        {url && Boolean(source.title) && <small>{url}</small>}
        {typeof source.finding==='string' && <KnowledgeText text={source.finding}/>}
      </article>
    })}</section>}
    {artifacts.length>0 && <section><h3>구현·검증 파일</h3>{artifacts.map((artifact,i)=><StoredValue key={i} value={artifact}/>)}</section>}
    {runs.length>0 && <section><h3>실행·측정 기록</h3>{runs.map((run,i)=><StoredValue key={i} value={run}/>)}</section>}
    {Object.keys(observation).length>0 && <section><h3>실제 반환 결과</h3><StoredValue value={observation}/></section>}
    {Object.keys(extra).length>0 && <section><h3>저장된 세부 값</h3><StoredValue value={extra}/></section>}
    {claim.object.kind==='literal' && !Object.keys(data).length && claim.object.value!==claim.statement && <section><h3>저장된 값{claim.object.unit ? ' · '+claim.object.unit : ''}</h3><StoredValue value={claim.object.value}/></section>}
    {typeof data.workspace==='string' && <details className="note-details"><summary>산출물이 남아 있는 작업공간</summary><code>{data.workspace}</code></details>}
    <details className="note-details"><summary>원본 기록 전체</summary><StoredValue value={claim}/></details>
  </article>
}
