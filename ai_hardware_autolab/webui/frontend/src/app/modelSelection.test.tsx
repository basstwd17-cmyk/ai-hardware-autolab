import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ModelPicker } from './ModelPicker'
import { readModelSelection, setModelChoice, withModelSelection, type ModelSelection } from './modelSelection'

describe('user model preferences', () => {
  it('survives storage and ignores invalid storage without exposing extra fields', () => {
    expect(readModelSelection('broken')).toEqual({})
    expect(readModelSelection('[]')).toEqual({})
    expect(readModelSelection('{"provider":"bad","builder":{"model":"test","token":"secret"}}'))
      .toEqual({ builder: { model: 'test' } })
    const choice = { default: { model: 'small', effort: 'low' } }
    expect(readModelSelection(JSON.stringify(choice))).toEqual(choice)
  })
  it('clears effort when changing models and supports inheriting the common model', () => {
    const choices = { default: { model: 'small' }, builder: { model: 'large', effort: 'high' } }
    expect(setModelChoice(choices, 'builder', 'small').builder).toEqual({ model: 'small' })
    expect(setModelChoice(choices, 'builder', '')).toEqual({ default: { model: 'small' } })
  })
  it('sends the displayed selection with a mission, without changing the scenario or authority', () => {
    const action = { type: 'job.mission', commandId: 'c', scenario: 'offline parser', scenarioSha256: 'abc' }
    const choice: ModelSelection = { default: { model: 'small', effort: 'low' } }
    const frame = withModelSelection(action, choice)
    expect(frame).toEqual({ ...action, modelSelection: choice })
    choice.default!.model = 'changed later'
    expect(frame.modelSelection).toEqual({ default: { model: 'small', effort: 'low' } })
    expect(withModelSelection(action, {})).toBe(action)
  })
  it('renders common and three role choices and preserves unavailable saved models visibly', () => {
    const html = renderToStaticMarkup(<ModelPicker api={null} selection={{ builder: { model: 'old-model', effort: 'high' } }} onChange={() => {}} />)
    expect(html).toContain('공통 모델 선택')
    expect(html).toContain('Lead · 조사·계획 선택')
    expect(html).toContain('Verifier · 검증 선택')
    expect(html).toContain('old-model · 목록 확인 필요')
    expect(html).toContain('새 작업부터 적용')
    expect(html).toContain('도구 직접 실행 단계는 모델을 호출하지 않습니다')
    expect(html).not.toContain('type="password"')
  })
})
