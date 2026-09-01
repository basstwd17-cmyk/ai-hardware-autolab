/**
 * The layout the user authored: loaded once, saved on a settle, merged on a clash.
 *
 * Two things this hook is careful about.
 *
 * **It debounces, and it separates "held" from "mine".** Dragging a node fires
 * a change per frame; saving each one would be a PUT per pixel. Positions
 * settle for a moment and then go out together. `mine` — the nodes dragged in
 * *this* tab — is what gets re-asserted on a conflict; `held` is everything
 * this tab is drawing, including what it read from the server at load.
 *
 * **A save that is still in flight is not raced.** A second settle while the
 * first PUT is open would send two saves against the same revision, and the
 * second would conflict with the first for no reason. So there is one save at
 * a time, and anything that arrives during it is sent after.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Positions } from '../api/client'
import { saveMerging, type LayoutApi } from './persistence'

export const SETTLE_MS = 400

export interface GraphLayoutState {
  held: Positions
  moved: number
  saving: boolean
  error: string | null
  /** A node the user dropped. */
  place: (id: string, x: number, y: number) => void
  /** Forget every manual position, here and on disk. */
  reset: () => void
}

export function useGraphLayout(api: LayoutApi | null): GraphLayoutState {
  const [held, setHeld] = useState<Positions>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const revision = useRef(0)
  const mine = useRef<Positions>({})
  const pending = useRef(false)
  const inFlight = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
      if (timer.current !== null) clearTimeout(timer.current)
    }
  }, [])

  useEffect(() => {
    if (!api) return
    void api
      .layout()
      .then((layout) => {
        if (!live.current) return
        revision.current = layout.revision
        setHeld(layout.positions)
        if (layout.unreadable) {
          // The file is there and this version cannot read it. Saying so beats
          // an empty canvas that looks like nothing was ever arranged.
          setError(`the saved layout was not applied: ${layout.unreadable}`)
        }
      })
      .catch((exc) => live.current && setError(String(exc)))
  }, [api])

  const flush = useCallback(async () => {
    if (!api || inFlight.current) return
    if (Object.keys(mine.current).length === 0) return
    inFlight.current = true
    setSaving(true)
    try {
      const saved = await saveMerging(api, revision.current, mine.current)
      revision.current = saved.revision
      if (live.current) {
        setHeld(saved.positions)
        setError(null)
      }
    } catch (exc) {
      if (live.current) setError(String(exc))
    } finally {
      inFlight.current = false
      if (live.current) setSaving(false)
      if (pending.current) {
        pending.current = false
        void flush()
      }
    }
  }, [api])

  const schedule = useCallback(() => {
    if (inFlight.current) {
      pending.current = true
      return
    }
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = setTimeout(() => void flush(), SETTLE_MS)
  }, [flush])

  const place = useCallback(
    (id: string, x: number, y: number) => {
      mine.current = { ...mine.current, [id]: { x, y } }
      setHeld((current) => ({ ...current, [id]: { x, y } }))
      schedule()
    },
    [schedule],
  )

  const reset = useCallback(() => {
    if (!api) return
    mine.current = {}
    setHeld({})
    setSaving(true)
    // An explicit clear, not a merge: the user asked for *no* manual
    // positions, and merging their absence onto someone else's copy would be
    // a contradiction. Whoever saves next wins, which is what "reset" means.
    void api
      .saveLayout(revision.current, {})
      .then(async (answer) => {
        if (answer.ok) {
          revision.current = answer.layout.revision
          return
        }
        const second = await api.saveLayout(answer.current.revision, {})
        if (second.ok) revision.current = second.layout.revision
      })
      .catch((exc) => live.current && setError(String(exc)))
      .finally(() => live.current && setSaving(false))
  }, [api])

  return { held, moved: Object.keys(held).length, saving, error, place, reset }
}
