import { useEffect, useState } from 'react'
import type { Api } from '../api/client'
import { setModelChoice, type ModelOption, type ModelRole, type ModelSelection } from './modelSelection'

// The same picker is mounted in Start and Settings. Share the metadata request,
// not credentials or choices; failed requests remain retryable.
const catalogs = new WeakMap<Api, { until: number; promise: Promise<ModelOption[]> }>()
function modelsFor(api: Api): Promise<ModelOption[]> {
  const cached = catalogs.get(api)
  if (cached && cached.until > Date.now()) return cached.promise
  const promise = api.command({ type: 'models.list', commandId: `models-${Math.random().toString(36).slice(2)}` })
    .then((result) => {
      if (!result.ok || !Array.isArray(result.models)) throw new Error(String(result.error ?? '모델 목록을 받지 못했습니다.'))
      return result.models as ModelOption[]
    }).catch((error: unknown) => { catalogs.delete(api); throw error })
  catalogs.set(api, { until: Date.now() + 300_000, promise })
  return promise
}

const labels: Record<ModelRole, string> = {
  default: '공통 모델', 'lead-agent': 'Lead · 조사·계획', builder: 'Builder · 구현', verifier: 'Verifier · 검증',
}

export function ModelPicker({ api, selection, onChange, saveError = '', disabled = false }: {
  api: Api | null; selection: ModelSelection; onChange: (value: ModelSelection) => void
  saveError?: string; disabled?: boolean
}) {
  const [models, setModels] = useState<ModelOption[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [loadVersion, setLoadVersion] = useState(0)
  useEffect(() => {
    if (!api || loadVersion === 0) return // Opening the platform must not require an extra CLI process.
    let live = true
    setLoading(true)
    setError('')
    modelsFor(api).then((rows) => { if (live) setModels(rows) })
      .catch((exc: unknown) => { if (live) setError(String(exc)) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [api, loadVersion])

  function row(role: ModelRole) {
    const choice = selection[role]
    const model = models.find((item) => item.model === choice?.model)
    return <div key={role} className="model-choice-row">
      <label className="scope-field">
        <span>{labels[role]}</span>
        <select aria-label={`${labels[role]} 선택`} value={choice?.model ?? ''} disabled={disabled}
          onChange={(event) => onChange(setModelChoice(selection, role, event.target.value))}>
          <option value="">{role === 'default' ? '데몬 / Codex 기본 설정' : '공통 설정 사용'}</option>
          {choice && !model && <option value={choice.model}>{choice.model} · 목록 확인 필요</option>}
          {models.map((item) => <option key={item.model} value={item.model}>{item.displayName} · {item.model}</option>)}
        </select>
      </label>
      {choice && <label className="scope-field">
        <span>추론 강도</span>
        <select aria-label={`${labels[role]} 추론 강도`} value={choice.effort ?? ''} disabled={disabled || !model}
          onChange={(event) => onChange({ ...selection, [role]: { model: choice.model,
            ...(event.target.value ? { effort: event.target.value } : {}) } })}>
          <option value="">모델 기본값{model?.defaultEffort ? ` · ${model.defaultEffort}` : ''}</option>
          {choice.effort && !model?.efforts.includes(choice.effort) && <option value={choice.effort}>{choice.effort} · 확인 필요</option>}
          {model?.efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
        </select>
      </label>}
    </div>
  }

  return <section className="model-picker" aria-label="에이전트 모델 설정">
    <div className="row"><h3>에이전트 모델</h3>
      <button className="chip" disabled={!api || loading || disabled} onClick={() => setLoadVersion((n) => n + 1)}>
        {loading ? '목록 확인 중…' : '모델 목록 불러오기'}
      </button>
    </div>
    {row('default')}
    <details><summary>에이전트별로 다르게 선택</summary>
      {(['lead-agent', 'builder', 'verifier'] as const).map(row)}
    </details>
    <p className="muted scope-note">새 작업부터 적용됩니다. 실행 중인 작업은 바뀌지 않으며, 선택값은 작업 기록에 저장됩니다.
      기본값은 기존 데몬 설정을 유지합니다. 도구 직접 실행 단계는 모델을 호출하지 않습니다.</p>
    <p className="muted scope-note">현재 Codex CLI 모델만 지원합니다. 추론 강도가 높으면 시간·사용량이 늘 수 있습니다.</p>
    {(error || saveError) && <p role="alert" className="error">{error || saveError}</p>}
    {Object.keys(selection).length > 0 && <button className="chip" disabled={disabled} onClick={() => onChange({})}>기본 설정으로 되돌리기</button>}
  </section>
}
