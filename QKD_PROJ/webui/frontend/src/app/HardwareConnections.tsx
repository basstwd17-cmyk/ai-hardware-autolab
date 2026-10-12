import { useEffect, useState } from 'react'
import type { Platform } from './usePlatform'
import { withHardwareSelection, type HardwareSelection } from './hardwareSelection'

export type ConnectionInventory = {
  status: string
  setup?: { title?: string; board_id: string; revision: string }
  checked_at?: string | null
  requested_devices?: string[]
  error?: string
  devices: {
    id?: string; kind: string; model?: string; host?: string; port?: string | number
    status: string; checked_at?: string | null; run?: string; detail?: string
  }[]
}

const labels: Record<string, string> = {
  not_checked: '미확인', checking: '확인 중', connected: '장비 식별 확인',
  unavailable: '식별 확인 실패', unsupported: '확인 드라이버 필요', error: '확인 오류',
  disabled: '연결 확인 사용 불가', checked: '확인 완료',
  cancelled: '확인 중단',
  present: 'PC에서 연결 확인 · 보드 기능 미검증',
}

export function ConnectionSummary({ inventory }: { inventory: ConnectionInventory }) {
  return <div aria-live="polite">
    <p>{inventory.setup?.title ?? '저장된 장비 구성'} · {labels[inventory.status] ?? inventory.status}</p>
    <ul>{inventory.devices.map((device, index) => <li key={`${device.kind}-${index}`}>
      {device.model ?? device.kind} {device.host ? `(${device.host}:${device.port})` : device.port ? `(${device.port})` : ''}
      {' — '}{labels[device.status] ?? device.status}
      {device.checked_at && <small> · {new Date(device.checked_at).toLocaleString()}</small>}
      {device.run && <small> · 기록: {device.run}</small>}
      {device.status !== 'connected' && device.detail && <p className="muted">{device.detail}</p>}
    </li>)}</ul>
    {inventory.checked_at && <p className="muted">마지막 확인 요청 완료: {new Date(inventory.checked_at).toLocaleString()}</p>}
    {inventory.error && <p className="error">{inventory.error}</p>}
  </div>
}

export function HardwareConnections({ api, selection }: { api: Platform['api']; selection?: HardwareSelection }) {
  const [inventory, setInventory] = useState<ConnectionInventory | null>(null)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [selected, setSelected] = useState<string[]>([])

  useEffect(() => {
    if (!api) return
    const abort = new AbortController()
    api.command(withHardwareSelection({ type: 'hardware.status' }, selection), abort.signal).then(result => {
      if (!abort.signal.aborted) {
        if (!result.ok) throw new Error(String(result.error))
        setInventory(result.inventory as ConnectionInventory)
      }
    }).catch(exc => { if (!abort.signal.aborted) setError(String(exc)) })
    return () => abort.abort()
  }, [api, selection])

  useEffect(() => {
    if (!api || inventory?.status !== 'checking' || error) return
    const abort = new AbortController()
    const timer = setTimeout(() => {
      api.command(withHardwareSelection({ type: 'hardware.status' }, selection), abort.signal).then(result => {
        if (!abort.signal.aborted) {
          if (!result.ok) throw new Error(String(result.error))
          setInventory(result.inventory as ConnectionInventory)
        }
      }).catch(exc => { if (!abort.signal.aborted) setError(String(exc)) })
    }, 1000)
    return () => { clearTimeout(timer); abort.abort() }
  }, [api, selection, inventory, error])

  const check = async () => {
    if (!api || sending || !selected.length) return
    setSending(true)
    setError('')
    try {
      const result = await api.command(withHardwareSelection({ type: 'hardware.check', devices: selected }, selection))
      if (!result.ok) throw new Error(String(result.error))
      setInventory(result.inventory as ConnectionInventory)
    } catch (exc) { setError(String(exc)) }
    finally { setSending(false) }
  }

  return <section aria-label="장비 연결 상태">
    {inventory && inventory.status !== 'disabled' && <fieldset disabled={sending || inventory.status === 'checking'}>
      <legend>직접 확인할 장비 (선택사항)</legend>
      {[...new Set(inventory.devices.map(device => device.id ?? device.kind))].map(kind => <label key={kind}>
        <input type="checkbox" checked={selected.includes(kind)} onChange={event => {
          setSelected(current => event.target.checked ? [...current, kind] : current.filter(value => value !== kind))
        }} />
        {inventory.devices.find(device => (device.id ?? device.kind) === kind)?.model ?? kind}
        {' · '}{inventory.devices.find(device => (device.id ?? device.kind) === kind)?.port ?? kind}
      </label>)}
    </fieldset>}
    <button className="chip" type="button" onClick={check}
      disabled={!api || !selected.length || sending || (inventory?.status === 'checking' && !error) || inventory?.status === 'disabled'}>
      {sending || (inventory?.status === 'checking' && !error) ? '장비 확인 중…' : '선택 장비 연결 확인'}
    </button>
    {inventory && <ConnectionSummary inventory={inventory} />}
    {error && <p className="error">{error}</p>}
    <p className="muted">에이전트가 시나리오에 필요한 장비만 확인하고 기존 결과를 재사용합니다.
      직접 확인하지 않아도 작업을 시작할 수 있습니다. 장비 식별 결과는 확인 시점의 상태이며,
      실제 배선과 기능 동작은 별도로 확인합니다.</p>
  </section>
}
