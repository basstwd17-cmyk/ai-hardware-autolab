/**
 * One place that knows what the page believes, and where each belief came from.
 *
 * The split mirrors the backend's, because copying it is what keeps the two
 * from disagreeing:
 *
 * * **`state`** is the control plane's answer — jobs, inbox, results. It is
 *   pushed as `webui.snapshot` and re-read on demand. The page never derives a
 *   job's state from a `job.*` event, for the same reason the backend does not.
 * * **`graph`** is re-read whenever `graph.changed` says the cursor moved.
 *   A notice, not a delta: patching a local copy is how a map ends up quietly
 *   disagreeing with the ledger.
 * * **`connection`** is what this page can honestly claim about itself.
 *   `resyncs` is displayed rather than hidden — a page that has been holed and
 *   recovered is in a different situation from one that never was.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Api,
  type DaemonStatus,
  type GraphSnapshot,
  type LedgerEvent,
  type OfficeSnapshot,
  type StateResponse,
} from '../api/client'
import { PlatformSocket, type Frame } from '../api/socket'

export type Liveness = 'connecting' | 'live' | 'retrying' | 'resyncing'

export interface Connection {
  liveness: Liveness
  daemon: DaemonStatus | null
  detail: string
  resyncs: number
  dropped: number
}

export interface Platform {
  /** The same client the views use, so there is one place the token lives. */
  api: Api | null
  connection: Connection
  state: StateResponse | null
  graph: GraphSnapshot | null
  /** Who is at a desk *now*. A different question from the graph's agents. */
  office: OfficeSnapshot | null
  /** Recent durable rows delivered by the live socket, for the activity feed. */
  liveEvents: LedgerEvent[]
  error: string | null
  refresh: () => void
}

const EMPTY: Connection = {
  liveness: 'connecting',
  daemon: null,
  detail: '',
  resyncs: 0,
  dropped: 0,
}

export function usePlatform(token: string | null): Platform {
  const api = useMemo(() => (token ? new Api(token) : null), [token])
  const [connection, setConnection] = useState<Connection>(EMPTY)
  const [state, setState] = useState<StateResponse | null>(null)
  const [graph, setGraph] = useState<GraphSnapshot | null>(null)
  const [office, setOffice] = useState<OfficeSnapshot | null>(null)
  const [liveEvents, setLiveEvents] = useState<LedgerEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  // The graph is fetched on a notice, and notices can arrive faster than a
  // fetch completes. A generation counter drops answers that a newer request
  // has already superseded, so a slow response cannot overwrite a fresh one.
  const generation = useRef(0)
  const officeGeneration = useRef(0)
  const socket = useRef<PlatformSocket | null>(null)

  const readGraph = useCallback(async () => {
    if (!api) return
    const mine = ++generation.current
    try {
      const next = await api.graph()
      if (mine === generation.current) setGraph(next)
    } catch (exc) {
      setError(String(exc))
    }
  }, [api])

  const readOffice = useCallback(async () => {
    if (!api) return
    const mine = ++officeGeneration.current
    try {
      const next = await api.office()
      if (mine === officeGeneration.current) setOffice(next)
    } catch (exc) {
      setError(String(exc))
    }
  }, [api])

  const readState = useCallback(async (): Promise<number> => {
    if (!api) return 0
    try {
      const next = await api.state()
      setState(next)
      setConnection((c) => ({ ...c, daemon: next.daemon }))
      return next.daemon.cursor
    } catch (exc) {
      setError(String(exc))
      return 0
    }
  }, [api])

  const refresh = useCallback(() => {
    void readState()
    void readGraph()
    void readOffice()
  }, [readState, readGraph, readOffice])

  useEffect(() => {
    if (!api) return
    let cancelled = false

    const live = new PlatformSocket((after) => api.wsUrl(after), {
      onOpen: () => setConnection((c) => ({ ...c, liveness: 'live', detail: '' })),
      onClose: (detail) => setConnection((c) => ({ ...c, liveness: 'retrying', detail })),
      onFrame: (frame: Frame) => {
        if (cancelled) return
        switch (frame.type) {
          case 'webui.hello':
          case 'webui.status':
            setConnection((c) => ({ ...c, daemon: frame.status as DaemonStatus }))
            break
          case 'webui.snapshot':
            // Pushed whenever the ledger moved. `/api/state` shapes it for
            // rendering, so the push is treated as "go and re-read".
            void readState()
            // The office empties a desk only when a job stops being open, and
            // that is what a new snapshot may have just decided.
            void readOffice()
            break
          case 'graph.changed':
            void readGraph()
            break
          case 'office.changed':
            void readOffice()
            break
          case 'ledger.event': {
            const event = frame.event as LedgerEvent | undefined
            if (event && typeof event.event_id === 'number' && typeof event.job_id === 'string') {
              setLiveEvents((previous) => {
                if (previous.some((row) => row.event_id === event.event_id)) return previous
                // This is a UI convenience cache, not a second ledger.  The
                // full history remains the transcript endpoint.
                return [...previous, event].slice(-600)
              })
            }
            break
          }
          default:
            break
        }
      },
      onResync: (cursor, dropped) => {
        // The contract: re-read the snapshots, then reconnect from the cursor
        // those snapshots actually cover -- never from the one in the notice,
        // which is the server's, not this page's.
        setConnection((c) => ({
          ...c,
          liveness: 'resyncing',
          resyncs: c.resyncs + 1,
          dropped: c.dropped + dropped,
          detail: `fell behind at cursor ${cursor}; re-reading`,
        }))
        // No activity after a declared gap may be presented as consecutive
        // with what came before it.  The resumed replay will fill this again.
        setLiveEvents([])
        void (async () => {
          const applied = await readState()
          await readGraph()
          await readOffice()
          if (!cancelled) live.connect(applied)
        })()
      },
    })
    socket.current = live

    void (async () => {
      const applied = await readState()
      await readGraph()
      await readOffice()
      if (!cancelled) live.connect(applied)
    })()

    return () => {
      cancelled = true
      live.close()
      socket.current = null
    }
  }, [api, readState, readGraph, readOffice])

  return { api, connection, state, graph, office, liveEvents, error, refresh }
}
