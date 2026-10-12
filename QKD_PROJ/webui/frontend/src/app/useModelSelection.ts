import { useEffect, useState } from 'react'
import { MODEL_CHANGE_EVENT, MODEL_STORAGE_KEY, readModelSelection, type ModelSelection } from './modelSelection'

function read(): ModelSelection {
  try { return readModelSelection(localStorage.getItem(MODEL_STORAGE_KEY)) }
  catch { return {} }
}

export function useModelSelection() {
  const [selection, setSelection] = useState(read)
  const [saveError, setSaveError] = useState('')
  useEffect(() => {
    const update = () => setSelection(read())
    window.addEventListener('storage', update)
    window.addEventListener(MODEL_CHANGE_EVENT, update)
    return () => {
      window.removeEventListener('storage', update)
      window.removeEventListener(MODEL_CHANGE_EVENT, update)
    }
  }, [])
  function change(next: ModelSelection) {
    try {
      localStorage.setItem(MODEL_STORAGE_KEY, JSON.stringify(next))
      setSaveError('')
      setSelection(next)
      window.dispatchEvent(new Event(MODEL_CHANGE_EVENT))
    } catch { setSaveError('브라우저 설정 저장이 차단되어 모델 선택을 저장하지 못했습니다.') }
  }
  return { selection, change, saveError }
}
