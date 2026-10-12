import {createRoot} from 'react-dom/client'
import {useMemo, useState} from 'react'
import {BrainView} from './BrainView'
import {Api, ApiError} from '../api/client'
import '../index.css'

class RemoteKnowledgeApi extends Api {
  constructor(private operator: string, private device: string, private onSync: (message: string) => void) {super('')}
  override async command(frame: Record<string, unknown>, signal?: AbortSignal) {
    if (frame.type !== 'knowledge.events') throw new Error('읽기 전용 지식맵입니다.')
    const params = new URLSearchParams({deviceId:this.device,after:String(frame.after ?? 0),limit:'100'})
    const response = await fetch(`/api/relay/knowledge?${params}`, {signal,headers:{authorization:`Bearer ${this.operator}`}})
    const body = await response.json()
    if (!response.ok) throw new ApiError(response.status, body.error || '지식 조회 실패')
    const sync = body.sync
    this.onSync(`${this.device} · ${sync.online ? '연결됨' : '오프라인 · 마지막 저장 자료'} · 동기화 ${sync.cursor}/${sync.latest}`)
    return body as Record<string, unknown>
  }
}

function RemoteMap() {
  const [token, setToken] = useState(sessionStorage.getItem('rtase.operatorToken') || '')
  const [device, setDevice] = useState(sessionStorage.getItem('rtase.deviceId') || 'lab-pc')
  const [connected, setConnected] = useState(Boolean(token))
  const [status, setStatus] = useState('지식 동기화 상태 확인 중')
  const api = useMemo(() => connected ? new RemoteKnowledgeApi(token,device,setStatus) : null,[connected,token,device])
  return <main className="remote-knowledge">
    <header><a href="/remote.html">← 플랫폼</a><strong>개발 지식맵</strong>
      <span>{connected ? status : '연구실 연결'}</span>
      {connected && <button onClick={()=>setConnected(false)}>연결 설정</button>}</header>
    {!connected ? <form className="pad prose" onSubmit={event=>{
      event.preventDefault(); sessionStorage.setItem('rtase.operatorToken',token.trim());
      sessionStorage.setItem('rtase.deviceId',device.trim()); setToken(token.trim());setDevice(device.trim());setConnected(true)
    }}><label>장치 이름<input required value={device} onChange={e=>setDevice(e.target.value)}/></label>
      <label>Operator token<input required type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)}/></label>
      <button>지식맵 열기</button></form> : <div className="map-stack"><BrainView api={api}/></div>}
  </main>
}
createRoot(document.getElementById('root')!).render(<RemoteMap/> )
