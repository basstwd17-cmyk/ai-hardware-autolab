import {useEffect, useState} from 'react'
import type {Api} from '../api/client'

export type Project = {project_id: string; name: string; root: string; description: string; build_command: string; test_command: string}
export const PROJECT_STORAGE_KEY = 'autolab.project.v1'

export function ProjectPicker({api, selected, onChange, disabled=false}: {
  api: Api | null; selected?: string; onChange: (id?: string) => void; disabled?: boolean
}) {
  const [projects, setProjects] = useState<Project[]>([])
  const [defaultId, setDefaultId] = useState('')
  const [revision, refresh] = useState(0)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [root, setRoot] = useState('')
  const [description, setDescription] = useState('')
  const [build, setBuild] = useState('')
  const [test, setTest] = useState('')
  useEffect(() => {
    if (!api) return
    const abort = new AbortController()
    api.command({type:'projects.catalog'}, abort.signal).then(result => {
      if (abort.signal.aborted) return
      if (!result.ok) throw new Error(String(result.error))
      setProjects(result.projects as Project[])
      setDefaultId(String(result.default_project_id))
      setError('')
    }).catch(cause => {if (!abort.signal.aborted) setError(String(cause))})
    return () => abort.abort()
  }, [api, revision])
  const current = projects.find(p => p.project_id === (selected || defaultId))
  const connect = async () => {
    if (!api || busy) return
    setBusy(true); setError('')
    try {
      const result = await api.command({type:'projects.register', project:{root:root.trim(), name:name.trim(), description, build_command:build, test_command:test}})
      if (!result.ok) throw new Error(String(result.error))
      const row = result.project as Project
      setProjects(rows => [...rows.filter(p=>p.project_id!==row.project_id), row])
      onChange(row.project_id)
    } catch (cause) {setError(String(cause))}
    finally {setBusy(false)}
  }
  return <section className="project-picker" aria-label="개발 프로젝트 연결">
    <label className="scope-field"><span>이번 작업의 프로젝트</span>
      <select value={selected ?? ''} disabled={disabled || busy} onChange={event=>onChange(event.target.value || undefined)}>
        <option value="">데몬 기본 프로젝트{projects.find(p=>p.project_id===defaultId)?.name ? ' · '+projects.find(p=>p.project_id===defaultId)!.name : ''}</option>
        {selected && !projects.some(p=>p.project_id===selected) && <option value={selected}>{selected} · 목록 확인 필요</option>}
        {projects.map(p=><option key={p.project_id} value={p.project_id}>{p.name}</option>)}
      </select>
    </label>
    {current && <div className="project-guide"><strong>{current.name}</strong><p>{current.root}</p>
      {current.description && <p>{current.description}</p>}
      {current.build_command && <p>빌드: <code>{current.build_command}</code></p>}
      {current.test_command && <p>검증: <code>{current.test_command}</code></p>}
    </div>}
    <p className="muted">프로젝트 소스를 별도 작업공간에서 개발합니다. 선택과 개발 안내는 작업마다 저장하며, 지식은 프로젝트별로 구분합니다.</p>
    <button type="button" className="link" disabled={!api || busy} onClick={()=>refresh(n=>n+1)}>프로젝트 목록 새로고침</button>
    <details><summary>내 프로젝트 연결 / 개발 안내 수정</summary>
      <fieldset disabled={disabled || busy || !api}>
        <legend>데몬이 실행되는 PC의 Git 저장소</legend>
        <label className="scope-field"><span>프로젝트 이름</span><input value={name} onChange={e=>setName(e.target.value)} placeholder="예: 실험 데이터 분석" /></label>
        <label className="scope-field"><span>소스 폴더의 절대 경로</span><input value={root} onChange={e=>setRoot(e.target.value)} placeholder="C:/Research/my-project" /></label>
        <label className="scope-field"><span>목적·참고 자료·개발 안내</span><textarea value={description} onChange={e=>setDescription(e.target.value)} /></label>
        <label className="scope-field"><span>빌드 방법 (선택)</span><input value={build} onChange={e=>setBuild(e.target.value)} placeholder="예: npm run build" /></label>
        <label className="scope-field"><span>검증 방법 (선택)</span><input value={test} onChange={e=>setTest(e.target.value)} placeholder="예: python -m pytest" /></label>
        <p className="muted">Git 저장소 최상위 폴더와 첫 커밋이 필요합니다. 같은 폴더를 다시 연결하면 개발 안내를 갱신합니다. 실행 방법은 에이전트에게 전달되는 안내입니다.</p>
        <button type="button" className="chip" onClick={connect} disabled={!root.trim()}>{busy ? '연결 중…' : '프로젝트 연결'}</button>
      </fieldset>
    </details>
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
