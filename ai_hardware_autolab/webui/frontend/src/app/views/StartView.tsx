/**
 * Ask the preflight, then open one durable mission.
 *
 * Every request opens the same Lead → Builder → Verifier conversation in the selected project.
 *
 * **A click is not a result.** The button reports what the daemon answered —
 * `command.result` — and the job appears in the other panels because the
 * ledger says it exists, not because this page thinks it started one.
 */

import { useRef, useState } from 'react'
import {
  scenarioWithScope,
  type SearchPolicy,
} from '../discovery'
import type { Platform } from '../usePlatform'
import { ProjectPicker, PROJECT_STORAGE_KEY } from '../ProjectPicker'
import { ModelPicker } from '../ModelPicker'
import { useModelSelection } from '../useModelSelection'
import { withModelSelection } from '../modelSelection'

export function StartView({ platform }: { platform: Platform }) {
  const models = useModelSelection()
  const [project, setProject] = useState<string | undefined>(() => {
    try { return localStorage.getItem(PROJECT_STORAGE_KEY) || undefined } catch { return undefined }
  })
  const [projectSaveError, setProjectSaveError] = useState('')
  const changeProject = (id?: string) => {
    setProject(id)
    try {
      if (id) localStorage.setItem(PROJECT_STORAGE_KEY, id)
      else localStorage.removeItem(PROJECT_STORAGE_KEY)
      setProjectSaveError('')
    } catch { setProjectSaveError('프로젝트 선택은 적용됐지만 브라우저에 저장하지 못했습니다.') }
  }
  const [scenario, setScenario] = useState('')
  const [searchPolicy, setSearchPolicy] = useState<SearchPolicy>('refresh')
  const [busy, setBusy] = useState<'send' | null>(null)
  const [answer, setAnswer] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [commandId] = useState(() => `webui-${Math.random().toString(36).slice(2, 10)}`)
  const submitting = useRef(false)

  const start = async () => {
    if (!platform.api || !scenario.trim() || submitting.current) return
    submitting.current = true
    setBusy('send')
    setFailed(null)
    setAnswer(null)
    try {
      const scoped = scenarioWithScope(scenario, 'offline')
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(scoped))
      const digest = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('')
      const result = await platform.api.command(withModelSelection({
        type: 'job.mission', commandId: `${commandId}-start-${Date.now()}`,
        objective: scenario.trim(), scenario: scoped, scenarioSha256: digest, searchPolicy,
        ...(project ? {projectId:project} : {}),
      }, models.selection))
      if (!result.ok) throw new Error(String(result.error ?? '작업 시작에 실패했습니다.'))
      const job = result.job as { job_id?: string } | undefined
      setAnswer(`작업을 접수했습니다: ${job?.job_id ?? ''}\nAgents 탭에서 진행 상황과 결과를 확인하세요.`)
      platform.refresh()
    } catch (exc) {
      setFailed(String(exc))
    } finally {
      submitting.current = false
      setBusy(null)
    }
  }

  return (
    <div className="pad start">
      <h2>무엇을 만들까요?</h2>
      <p className="muted">
        시나리오를 입력하면 Lead가 방향을 정하고, Builder가 구현하고, Verifier가 결과를 검토합니다.
        부족한 부분은 다시 전달해 수정합니다.
      </p>
      <p className="muted" role="note">선택한 프로젝트의 격리된 작업공간에서 구현하고 검증합니다.</p>

      <ProjectPicker api={platform.api} selected={project} onChange={changeProject} disabled={busy !== null} />
      {projectSaveError && <p className="error">{projectSaveError}</p>}
      <label className="scope-field">
        <span>Research policy</span>
        <select
          disabled
          value={searchPolicy}
          onChange={(event) => {
            setSearchPolicy(event.target.value as SearchPolicy)
            setAnswer(null)
          }}
        >
          <option value="refresh">Refresh — reuse known facts, research changed or unknown parts</option>
          <option value="always">Always search — refresh every mission question</option>
        </select>
      </label>
      <p className="muted scope-note">
        로컬 코드·문서·저장된 지식을 활용하고, 필요한 공식 문서는 웹 검색으로 조사합니다.
      </p>

      <ModelPicker api={platform.api} selection={models.selection} onChange={models.change}
        saveError={models.saveError} disabled={busy !== null} />

      <textarea
        className="scenario"
        rows={4}
        placeholder="원하는 기능과 확인할 결과를 편하게 적어주세요. 참고할 코드·자료가 있으면 함께 알려주세요."
        value={scenario}
        onChange={(event) => {
          setScenario(event.target.value)
          setAnswer(null)
        }}
      />
      <div className="row">
        <button className="chip" onClick={start} disabled={busy !== null || !scenario.trim() || !platform.api}>
          {busy === 'send' ? '시작 중…' : '작업 시작'}
        </button>
      </div>

      {failed && <p className="error">{failed}</p>}
      {answer && <pre className="answer">{answer}</pre>}
    </div>
  )
}
