/** Offline fixture only. No API, websocket, daemon or hardware connection. */
import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { OfficeCanvas } from '../src/office/OfficeCanvas'
import { AgentsView } from '../src/app/views/AgentsView'
import type { Platform } from '../src/app/usePlatform'
import type { Api, LedgerEvent } from '../src/api/client'
import { useTranscript } from '../src/app/useTranscript'
import '../src/index.css'

const messages: LedgerEvent[] = [
  ['lead-agent', 'webSearch', { action: { query: 'AM335x PRU timing datasheet' } }],
  ['lead-agent', 'agentMessage', { text: '핀 설정을 확인했습니다. 기존 PRU 코드를 확인하겠습니다.' }],
  ['builder', 'commandExecution', { command: 'Get-Content pru_main.c', exitCode: 0 }],
  ['builder', 'fileChange', { changes: [{ path: 'pru_main.c' }, { path: 'top.v' }] }],
  ['verifier', 'commandExecution', { command: 'py -3 -m pytest', exitCode: 1 }],
].map(([agent, type, item], index) => ({
  event_id: index + 1, seq: index + 1, job_id: 'offline-smoke', at: `2026-09-04T11:00:0${index}Z`,
  agent: agent as string, kind: 'runtime.item/completed',
  payload: { method: 'item/completed', item_type: type, params: { item: { type, ...item as object } } },
}))
const catalog = ['lead-agent', 'builder', 'verifier'].map((name, i) => ({
  name, team: ['lead', 'builder', 'verifier'][i], role: ['plan', 'build', 'verify'][i],
  description: 'Offline UI fixture', skills: [], consumes: [], emits: [], mcpTools: [], localTools: [],
  touchesHardware: false, nativeWeb: false,
}))
let requests = 0
const api = {
  agents: async () => ({ agents: catalog }), handoffs: async () => [],
  job: async (_id: string, since: number) => {
    requests++
    await new Promise((resolve) => setTimeout(resolve, 80))
    return { job: { job_id: 'offline-smoke', state: 'running', iteration: 1 }, events: messages.filter((r) => r.event_id > since), cursor: 5, latest: 5 }
  },
  command: async () => { throw new Error('Offline fixture: commands forbidden') },
}
function App() {
  const [working, setWorking] = useState(true), [panel, setPanel] = useState(true)
  const snapshot = useMemo(() => ({ cursor: 5, openJobs: ['offline-smoke'], occupants: catalog.map((c) => ({
    id: `offline-smoke:${c.name}`, jobId: 'offline-smoke', agent: c.name,
    activity: working ? c.name === 'verifier' ? 'reading' : 'typing' : 'waiting',
    detail: 'Offline fixture', seenAt: 5, turns: 1,
  })) }), [working])
  const platform = {
    api, state: { jobs: [{ job_id: 'offline-smoke', state: 'running', objective: '오프라인 UI 회귀 검증' }], results: {}, inbox: [] },
    connection: { liveness: 'live', daemon: { cursor: 5 }, detail: '', resyncs: 0, dropped: 0 },
    office: snapshot, graph: null, liveEvents: [], error: null, refresh: () => {},
  } as unknown as Platform
  return <>
    <header style={{ padding: 12, display: 'flex', gap: 12 }}>
      <b>OFFLINE UI TEST · 장비 연결 없음</b>
      <button id="work" onClick={() => setWorking(true)}>작업 시작</button>
      <button id="rest" onClick={() => setWorking(false)}>대기 · 자유 이동</button>
      <button id="panel" onClick={() => setPanel(!panel)}>탭 전환</button>
      <span>history requests: {requests}</span>
      <HistoryStress />
    </header>
    <main style={{ display: 'grid', gridTemplateColumns: '55% 45%', height: 'calc(100vh - 55px)' }}>
      <div style={{ display: 'grid', alignItems: 'center', background: '#090c10', padding: 12 }}><OfficeCanvas snapshot={snapshot} /></div>
      <div style={{ overflowY: 'auto', background: '#161b22' }}>{panel ? <AgentsView platform={platform} /> : <p>다른 패널 · 이전 기록은 원장에 남아 있습니다.</p>}</div>
    </main>
  </>
}

function HistoryStress() {
  const [cursor, setCursor] = useState(1), [caught, setCaught] = useState(false)
  const transcript = useTranscript(api as unknown as Api, 'offline-smoke', cursor)
  useEffect(() => {
    const tick = setInterval(() => setCursor((c) => c + 1), 20)
    const stop = setTimeout(() => clearInterval(tick), 1200)
    return () => { clearInterval(tick); clearTimeout(stop) }
  }, [])
  useEffect(() => {
    if (cursor > 1 && cursor < 50 && transcript.events.length === 5) setCaught(true)
  }, [cursor, transcript.events])
  return <span id="history-stress" data-caught={caught}>history under traffic: {caught ? 'OK' : 'checking'}</span>
}
createRoot(document.getElementById('root')!).render(<App />)
