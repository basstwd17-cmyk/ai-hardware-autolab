// Development-only visual fixture. No daemon, credentials, device calls or DB writes.
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import { BrainView } from '../src/brain/BrainView'
import { Api } from '../src/api/client'
import '../src/index.css'
const texts = ['BBB PRU DMA SPI firmware', 'STM32G474 HRTIM DMA waveform',
  'RTASE V1 CPLD DAC RF1 RF2', 'RTM3004 SCPI waveform', 'CPLD oscillator power supply datasheet']
const events = texts.map((statement, i) => ({event_id: i + 1, at: '2026-09-09', kind: 'knowledge.proposed',
  claim_id: `demo-${i}`, version_id: `demo-v${i}`, payload: {candidate: {
    subject: {kind: 'decision', id: `demo-${i}`, label: '시각 검증용 예제'}, relation: 'proposes',
    object: {kind: 'literal', value: statement}, statement, tags: ['demo'], evidence: [],
  }}}))
let disconnected = false
const api = new Api('offline-fixture')
api.command = async frame => {
  if (disconnected) throw new TypeError('Failed to fetch')
  return {ok: true, knowledge: {events: events.filter(e => e.event_id > Number(frame.after)), cursor: 5, latest: 5}}
}
function Preview() {
  const [show, setShow] = useState(true)
  return <main style={{height: '100vh', display: 'flex', flexDirection: 'column'}}>
    <header style={{padding: 10}}>시각 검증용 예제 · 실제 장비/지식 아님
      <button onClick={() => setShow(!show)}>탭 전환 테스트</button>
      <button onClick={() => { disconnected = !disconnected }}>연결 끊김 테스트</button>
    </header>
    {show && <div className="map-stack" style={{flex: 1, minHeight: 0}}><BrainView api={api} /></div>}
  </main>
}
createRoot(document.getElementById('root')!).render(<Preview />)
