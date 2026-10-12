/**
 * Ask the preflight, then open one durable mission.
 *
 * Every request opens the same Lead → Builder → Verifier conversation. In
 * Hardware mode the daemon binds its installed board profile.
 *
 * **A click is not a result.** The button reports what the daemon answered —
 * `command.result` — and the job appears in the other panels because the
 * ledger says it exists, not because this page thinks it started one.
 */

import { useEffect, useRef, useState } from 'react'
import {
  scenarioWithScope,
  type ExecutionScope,
  type SearchPolicy,
} from '../discovery'
import type { Platform } from '../usePlatform'
import { ProjectPicker, PROJECT_STORAGE_KEY } from '../ProjectPicker'
import { ModelPicker } from '../ModelPicker'
import { useModelSelection } from '../useModelSelection'
import { withModelSelection } from '../modelSelection'
import { HardwareConnections } from '../HardwareConnections'
import { HardwareSetups } from '../HardwareSetups'
import { HARDWARE_STORAGE_KEY, hardwareKey, parseHardwareSelection, withHardwareSelection, type HardwareSelection } from '../hardwareSelection'

export function StartView({ platform }: { platform: Platform }) {
  const models = useModelSelection()
  const [project, setProject] = useState<string | undefined>(() => {
    try { return localStorage.getItem(PROJECT_STORAGE_KEY) || undefined } catch { return undefined }
  })
  const [projectSaveError, setProjectSaveError] = useState('')
  const scopeChosen = useRef(false)
  const changeProject = (id?: string) => {
    setProject(id)
    if (id) { setScope('offline'); scopeChosen.current = true }
    try {
      if (id) localStorage.setItem(PROJECT_STORAGE_KEY, id)
      else localStorage.removeItem(PROJECT_STORAGE_KEY)
      setProjectSaveError('')
    } catch { setProjectSaveError('프로젝트 선택은 적용됐지만 브라우저에 저장하지 못했습니다.') }
  }
  const [hardware, setHardware] = useState<HardwareSelection | undefined>(() => {
    try { return parseHardwareSelection(localStorage.getItem(HARDWARE_STORAGE_KEY)) } catch { return undefined }
  })
  const [hardwareSaveError, setHardwareSaveError] = useState('')
  const changeHardware = (value?: HardwareSelection) => {
    setHardware(value)
    try {
      if (value) localStorage.setItem(HARDWARE_STORAGE_KEY, JSON.stringify(value))
      else localStorage.removeItem(HARDWARE_STORAGE_KEY)
      setHardwareSaveError('')
    } catch { setHardwareSaveError('이 브라우저에 선택을 저장하지 못했습니다. 이번 작업의 선택은 적용됩니다.') }
  }
  const [scenario, setScenario] = useState('')
  const [scope, setScope] = useState<ExecutionScope>(() => platform.connection?.daemon?.profile === 'hardware' && !project ? 'hardware' : 'offline')
  useEffect(() => {
    if (!scopeChosen.current && platform.connection?.daemon?.profile === 'offline') setScope('offline')
  }, [platform.connection?.daemon?.profile])
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
      const scoped = scenarioWithScope(scenario, scope)
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(scoped))
      const digest = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('')
      const result = await platform.api.command(withHardwareSelection(withModelSelection({
        type: 'job.mission', commandId: `${commandId}-start-${Date.now()}`,
        objective: scenario.trim(), scenario: scoped, scenarioSha256: digest, searchPolicy,
        ...(project ? {projectId:project} : {}),
      }, models.selection), scope === 'hardware' ? hardware : undefined))
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
      <p className="muted" role="note">
        {scope === 'offline'
          ? '격리된 작업공간에서 개발하고 검증합니다. Offline에서는 장비에 접근하지 않습니다.'
          : 'Builder의 장비 요청은 플랫폼이 연결·권한·안전 설정을 확인한 뒤 실행합니다. 정보가 부족하면 같은 작업에서 질문하고 답변 후 이어갑니다.'}
      </p>

      <ProjectPicker api={platform.api} selected={project} onChange={changeProject} disabled={busy !== null} />
      {projectSaveError && <p className="error">{projectSaveError}</p>}
      <label className="scope-field">
        <span>실행 모드</span>
        <select
          value={scope}
          onChange={(event) => {
            scopeChosen.current = true
            setScope(event.target.value as ExecutionScope)
            setAnswer(null)
          }}
        >
          {platform.connection?.daemon?.profile === 'hardware' && <option value="hardware">Hardware — 시나리오에 요청한 장비 제어 허용</option>}
          <option value="offline">일반 개발 — 소스·문서·실행·검증 (장비 사용 안 함)</option>
        </select>
      </label>
      <p className="muted scope-note">
        {scope === 'hardware' ? 'Hardware 작업을 시작하면 시나리오에 요청한 보드·스코프 제어를 이 job에 위임합니다. 선택한 구성에서 제공하는 연결 확인·통신·업로드·측정 도구를 자동 실행합니다.' : '선택한 프로젝트의 소스와 문서를 읽고 구현·실행·검증합니다. 모델은 그대로 사용하며 보드 등록은 필요 없습니다.'}
      </p>
      {scope === 'hardware' && <HardwareSetups api={platform.api} selection={hardware} onChange={changeHardware} disabled={busy !== null} />}
      {hardwareSaveError && <p className="error">{hardwareSaveError}</p>}
      {scope === 'hardware' && <HardwareConnections key={hardwareKey(hardware)} api={platform.api} selection={hardware} />}

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
