/**
 * The office holds the middle; everything else docks beside it.
 *
 * This is a change from the first shape, where the office was one full-screen
 * tab among four. Four equal tabs made the office a *place you go*, and the
 * point of it is that it is a place you are — the animated state of the run,
 * on screen while you read about it. So the office is the stage, permanently,
 * and the panels that explain it sit alongside rather than replacing it.
 *
 * The dock holds:
 *
 * * **Agents** — the run as a conversation, per agent, out of the ledger.
 * * **Map** — the same job as a graph of what it touched and why.
 * * **Inbox** — approvals waiting on a person, and what this host refused.
 * * **Settings** — what this page is attached to and what it can claim.
 *
 * Its side, width and open state are a browser preference, not platform state
 * (`useDock`). The divider is draggable, and the dock can take the whole width
 * when the map wants room, because a provenance graph in a 460px column is a
 * list with lines on it.
 */

import { useState } from 'react'
import { GraphView } from '../graph/GraphView'
import { BrainView } from '../brain/BrainView'
import type { LayoutApi } from '../graph/persistence'
import { useDock } from './useDock'
import type { Platform } from './usePlatform'
import { AgentsView } from './views/AgentsView'
import { StartView } from './views/StartView'
import { InboxView } from './views/InboxView'
import { OfficeStage } from './views/OfficeStage'
import { SettingsView } from './views/SettingsView'

const TABS = ['Agents', 'Start', 'Map', 'Inbox', 'Settings'] as const
type Tab = (typeof TABS)[number]

export function Shell({
  platform,
  token,
  api,
}: {
  platform: Platform
  token: string | null
  api: LayoutApi | null
}) {
  const [tab, setTab] = useState<Tab>('Agents')
  const [full, setFull] = useState(false)
  const [mapMode, setMapMode] = useState<'run' | 'brain'>('brain')
  const { dock, setSide, toggle, startResize } = useDock()

  const inbox = platform.state?.inbox.length ?? 0
  const showStage = dock.open ? !full : true

  return (
    <div className={`shell ${dock.side}`}>
      <header className="bar">
        <div className="brand">
          RTASE <span>agent platform</span>
        </div>
        <ConnectionBar platform={platform} />
        <button className="tab" onClick={toggle} title="show or hide the panel">
          {dock.open ? '▸ panel' : '◂ panel'}
        </button>
      </header>

      <div className="body">
        {showStage && (
          <div className="stage">
            <OfficeStage platform={platform} />
          </div>
        )}

        {dock.open && (
          <>
            {showStage && (
              <div
                className="divider"
                onPointerDown={startResize}
                role="separator"
                aria-label="resize the panel"
              />
            )}
            <aside className="dock" style={full ? undefined : { width: dock.width }}>
              <nav className="dock-tabs">
                {TABS.map((name) => (
                  <button
                    key={name}
                    className={tab === name ? 'tab on' : 'tab'}
                    onClick={() => setTab(name)}
                  >
                    {name}
                    {name === 'Inbox' && inbox > 0 && <b className="badge">{inbox}</b>}
                  </button>
                ))}
                <span className="dock-tools">
                  <button
                    className="link"
                    onClick={() => setFull(!full)}
                    title={full ? 'give the office its stage back' : 'let the panel take the width'}
                  >
                    {full ? 'restore' : 'expand'}
                  </button>
                  <button
                    className="link"
                    onClick={() => setSide(dock.side === 'right' ? 'left' : 'right')}
                    title="move the panel to the other side"
                  >
                    {dock.side === 'right' ? '⇤' : '⇥'}
                  </button>
                </span>
              </nav>

              <div className={`dock-body dock-${tab.toLowerCase()}`}>
                {platform.error && <div className="error">{platform.error}</div>}
                {/*
                  Agents and Start are work surfaces, not disposable views.
                  Keep them mounted while another tab is open so the selected
                  job, loaded transcript, scroll position and an unfinished
                  scenario do not vanish merely because the operator checked
                  the map or inbox.
                */}
                <div className="persistent-pane" hidden={tab !== 'Agents'}>
                  <AgentsView platform={platform} />
                </div>
                <div className="persistent-pane" hidden={tab !== 'Start'}>
                  <StartView platform={platform} />
                </div>
                {tab === 'Map' && (
                  <div className="map-stack">
                    <div className="map-switch">
                      <button className={`chip ${mapMode === 'brain' ? 'on' : ''}`} onClick={() => setMapMode('brain')}>지식 마인드맵</button>
                      <button className={`chip ${mapMode === 'run' ? 'on' : ''}`} onClick={() => setMapMode('run')}>실행 이력 · 상세</button>
                    </div>
                    {mapMode === 'run' ? <GraphView snapshot={platform.graph} api={api} /> : <BrainView api={platform.api} />}
                  </div>
                )}
                {tab === 'Inbox' && <InboxView platform={platform} />}
                {tab === 'Settings' && <SettingsView platform={platform} token={token} />}
              </div>
            </aside>
          </>
        )}
      </div>
    </div>
  )
}

function ConnectionBar({ platform }: { platform: Platform }) {
  const { liveness, daemon, detail, resyncs } = platform.connection
  const daemonUp = daemon?.connected ?? false

  return (
    <div className="conn" title={detail}>
      <span className={`dot ${liveness}`} />
      <span className="conn-text">
        {liveness === 'live' && daemonUp && `live · cursor ${daemon?.cursor ?? 0}`}
        {liveness === 'live' && !daemonUp && 'connected, no daemon'}
        {liveness === 'connecting' && 'connecting…'}
        {liveness === 'retrying' && `reconnecting — ${detail}`}
        {liveness === 'resyncing' && 're-reading after falling behind'}
      </span>
      {daemon?.profile && <span className={`runtime-mode ${daemon.profile}`}>{daemon.profile}</span>}
      {daemon?.workspace_mode && (
        <span className={`runtime-mode ${daemon.workspace_mode}`}>{daemon.workspace_mode}</span>
      )}
      {daemon && <span className="conn-addr">{daemon.address}</span>}
      {daemon?.gaps ? <span className="warn">{daemon.gaps} gap(s) replayed</span> : null}
      {resyncs > 0 && <span className="warn">{resyncs} resync(s)</span>}
      <button className="link" onClick={platform.refresh}>
        refresh
      </button>
    </div>
  )
}
