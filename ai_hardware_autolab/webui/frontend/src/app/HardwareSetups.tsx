import { useEffect, useState } from 'react'
import type { Platform } from './usePlatform'
import { hardwareKey, type HardwareSelection } from './hardwareSelection'

type Setup = HardwareSelection & {
  title?: string; model?: string; source_paths?: string[]; datasheets?: string[]
  devices: { id?: string; kind: string; model?: string; port?: string | number }[]
}
type Candidate = { candidate_id: string; kind: string; port?: string; description?: string; vid?: number; pid?: number }
type Discovery = { checked_at: string; devices: Candidate[]; errors: { kind: string; error: string }[] }
const lines = (value: string) => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)

export function HardwareSetups({ api, selection, onChange, disabled = false }: {
  api: Platform['api']; selection?: HardwareSelection
  onChange: (selection?: HardwareSelection) => void; disabled?: boolean
}) {
  const [setups, setSetups] = useState<Setup[]>([])
  const [defaultSetup, setDefaultSetup] = useState<HardwareSelection | null>(null)
  const [version, setVersion] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [discovery, setDiscovery] = useState<Discovery | null>(null)
  const [candidates, setCandidates] = useState<string[]>([])
  const [boardId, setBoardId] = useState('')
  const [revision, setRevision] = useState('v1.0')
  const [title, setTitle] = useState('')
  const [model, setModel] = useState('')
  const [sources, setSources] = useState('')
  const [datasheets, setDatasheets] = useState('')
  const [notes, setNotes] = useState('')
  const [serialPort, setSerialPort] = useState('')
  const [scopeFrom, setScopeFrom] = useState('')

  useEffect(() => {
    if (!api) return
    const abort = new AbortController()
    api.command({ type: 'hardware.catalog' }, abort.signal).then(result => {
      if (abort.signal.aborted) return
      if (!result.ok) throw new Error(String(result.error))
      setSetups(result.setups as Setup[])
      setDefaultSetup(result.default as HardwareSelection | null)
      setError('')
    }).catch(exc => { if (!abort.signal.aborted) setError(String(exc)) })
    return () => abort.abort()
  }, [api, version])

  const selected = setups.find(row => hardwareKey(row) === hardwareKey(selection ?? defaultSetup))
  const scan = async () => {
    if (!api || busy) return
    setBusy(true); setError('')
    try {
      const result = await api.command({ type: 'hardware.discover' })
      if (!result.ok) throw new Error(String(result.error))
      const found = result.discovery as Discovery
      setDiscovery(found)
      setCandidates(current => current.filter(id => found.devices.some(row => row.candidate_id === id)))
    } catch (exc) { setError(String(exc)) }
    finally { setBusy(false) }
  }

  const register = async () => {
    if (!api || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const scope = setups.find(row => hardwareKey(row) === scopeFrom)
      const result = await api.command({ type: 'hardware.register', setup: {
        board_id: boardId.trim(), revision: revision.trim(), title: title.trim(), model: model.trim(),
        source_paths: lines(sources), datasheets: lines(datasheets), notes,
        candidate_ids: candidates, ...(serialPort.trim() ? { serial_port: serialPort.trim() } : {}),
        ...(scope ? { scope_from: { board_id: scope.board_id, revision: scope.revision } } : {}),
      } })
      if (!result.ok) throw new Error(String(result.error))
      const setup = result.setup as Setup
      onChange({ board_id: setup.board_id, revision: setup.revision })
      setSetups(current => [...current.filter(row => hardwareKey(row) !== hardwareKey(setup)), setup])
      setNotice(`${setup.title ?? setup.board_id} 구성을 등록하고 선택했습니다.`)
    } catch (exc) { setError(String(exc)) }
    finally { setBusy(false) }
  }

  return <section aria-label="보드 및 장비 구성">
    <label className="scope-field"><span>이번 작업의 보드·장비 구성</span>
      <select value={hardwareKey(selection)} disabled={disabled || busy} onChange={event => {
        const row = setups.find(item => hardwareKey(item) === event.target.value)
        onChange(row ? { board_id: row.board_id, revision: row.revision } : undefined)
      }}>
        <option value="">기본 구성 사용{defaultSetup ? ` · ${hardwareKey(defaultSetup)}` : ''}</option>
        {selection && !setups.some(row => hardwareKey(row) === hardwareKey(selection))
          && <option value={hardwareKey(selection)}>{hardwareKey(selection)} · 목록 확인 필요</option>}
        {setups.map(row => <option key={hardwareKey(row)} value={hardwareKey(row)}>{row.title ?? row.board_id} · {row.revision}</option>)}
      </select>
    </label>
    <button className="chip" type="button" disabled={!api || busy} onClick={() => setVersion(value => value + 1)}>구성 목록 새로고침</button>
    {selected && <p className="muted">{selected.model ?? selected.title}
      {selected.source_paths?.length ? ` · 소스: ${selected.source_paths.join(', ')}` : ''}
      {selected.devices.some(row => row.kind === 'serial' || row.kind === 'usb')
        ? ' · 소스 개발·연결 확인을 지원합니다. 시리얼 포트는 통신할 수 있으며, 펌웨어 업로드에는 해당 보드의 프로그래머 실행기가 필요합니다.' : ''}
    </p>}
    <p className="muted">선택한 구성은 새 작업에 저장됩니다. 진행 중인 작업의 대상은 바뀌지 않습니다.</p>
    <details><summary>다른 보드 등록</summary>
      <fieldset disabled={disabled || busy || !api}>
        <legend>보드 정보와 연결</legend>
        <label className="scope-field"><span>구성 이름</span><input value={title} onChange={event => setTitle(event.target.value)} placeholder="예: STM32 개발 보드와 스코프" /></label>
        <label className="scope-field"><span>보드 모델</span><input value={model} onChange={event => setModel(event.target.value)} placeholder="보드에 표시된 정확한 모델" /></label>
        <label className="scope-field"><span>저장 ID (영문 소문자·숫자·하이픈)</span><input value={boardId} onChange={event => setBoardId(event.target.value)} placeholder="my-board" /></label>
        <label className="scope-field"><span>구성 리비전</span><input value={revision} onChange={event => setRevision(event.target.value)} /></label>
        <label className="scope-field"><span>소스 경로 (선택, 프로젝트 기준 한 줄에 하나)</span><textarea value={sources} onChange={event => setSources(event.target.value)} placeholder="STM32Cube/..." /></label>
        <label className="scope-field"><span>데이터시트·보드 문서 URL (선택, 한 줄에 하나)</span><textarea value={datasheets} onChange={event => setDatasheets(event.target.value)} /></label>
        <button className="chip" type="button" onClick={scan}>연결된 USB·시리얼 장치 찾기</button>
        {discovery && <div aria-live="polite">
          <p>{new Date(discovery.checked_at).toLocaleString()} · {discovery.devices.length}개 장치</p>
          {discovery.devices.map(row => <label className="scope-field" key={row.candidate_id}>
            <span><input type="checkbox" checked={candidates.includes(row.candidate_id)} onChange={event => setCandidates(current =>
              event.target.checked ? [...current, row.candidate_id] : current.filter(id => id !== row.candidate_id))} />
              {row.description ?? row.kind} · {row.port ?? 'USB'}
              {row.vid !== undefined && row.pid !== undefined ? ` · ${row.vid.toString(16).padStart(4, '0')}:${row.pid.toString(16).padStart(4, '0')}` : ''}
            </span>
          </label>)}
          {discovery.errors.map(row => <p className="error" key={row.kind}>{row.kind} 탐색 실패: {row.error}</p>)}
          {!discovery.devices.length && <p>발견된 장치가 없습니다. 연결 없이 소스 개발용으로도 등록할 수 있습니다.</p>}
        </div>}
        <label className="scope-field"><span>시리얼 포트 직접 지정 (선택)</span><input value={serialPort} onChange={event => setSerialPort(event.target.value)} placeholder="COM4" /></label>
        <label className="scope-field"><span>함께 사용할 기존 스코프 (선택)</span><select value={scopeFrom} onChange={event => setScopeFrom(event.target.value)}>
          <option value="">사용하지 않음</option>
          {setups.filter(row => row.devices.some(device => device.kind === 'scope')).map(row =>
            <option key={hardwareKey(row)} value={hardwareKey(row)}>{row.title ?? row.board_id} · {row.revision}</option>)}
        </select></label>
        <label className="scope-field"><span>개발 환경·배선 메모 (선택)</span><textarea value={notes} onChange={event => setNotes(event.target.value)} /></label>
        <p className="muted">탐색은 PC에 보이는 연결 정보를 읽습니다. 보드 모델·배선은 자동 확정하지 않습니다.
          스코프 배선은 시나리오에 CH1=PWM처럼 지정합니다. 새 구성에는 기존 보드의 배선을 복사하지 않습니다.</p>
        <button className="chip" type="button" onClick={register} disabled={!boardId.trim() || !revision.trim() || !title.trim() || !model.trim()}>구성 등록하고 선택</button>
      </fieldset>
    </details>
    {busy && <p role="status">처리 중…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
