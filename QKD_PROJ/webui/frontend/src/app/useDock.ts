/**
 * The dock beside the office: which side, how wide, and whether it is there.
 *
 * This is a per-person, per-browser preference, not platform state, so it
 * lives in `localStorage` rather than on the server. Nothing about it needs to
 * be shared between machines and nothing else needs to read it — putting it
 * behind an API would make a preference into a synchronised fact for no gain.
 *
 * Every read and write is guarded: private windows and browsers configured to
 * block site data throw on access rather than returning null, and a layout
 * preference is not worth a blank screen.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const KEY = 'qkd.webui.dock'

export type DockSide = 'left' | 'right'

export interface DockState {
  side: DockSide
  width: number
  open: boolean
}

export const MIN_WIDTH = 320
export const MAX_WIDTH = 1100
const DEFAULT: DockState = { side: 'right', width: 460, open: true }

/** Clamp on the way in as well as on the way out: a stored width from a wider
 *  monitor must not push the office off the screen on a narrower one. */
export function clampWidth(width: number, viewport: number): number {
  const ceiling = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, viewport - 360))
  return Math.min(ceiling, Math.max(MIN_WIDTH, Math.round(width)))
}

export function readDock(raw: string | null, viewport: number): DockState {
  if (!raw) return DEFAULT
  try {
    const parsed = JSON.parse(raw) as Partial<DockState>
    return {
      side: parsed.side === 'left' ? 'left' : 'right',
      width: clampWidth(typeof parsed.width === 'number' ? parsed.width : DEFAULT.width, viewport),
      open: parsed.open !== false,
    }
  } catch {
    return DEFAULT
  }
}

export function useDock() {
  const [dock, setDock] = useState<DockState>(DEFAULT)
  const dragging = useRef(false)

  useEffect(() => {
    try {
      setDock(readDock(window.localStorage.getItem(KEY), window.innerWidth))
    } catch {
      setDock(DEFAULT)
    }
  }, [])

  const remember = useCallback((next: DockState) => {
    setDock(next)
    try {
      window.localStorage.setItem(KEY, JSON.stringify(next))
    } catch {
      // Storage disabled. The dock still works for this session.
    }
  }, [])

  const setSide = useCallback((side: DockSide) => remember({ ...dock, side }), [dock, remember])
  const toggle = useCallback(() => remember({ ...dock, open: !dock.open }), [dock, remember])

  /** Drag the divider. Width is derived from the pointer and the dock's side. */
  const startResize = useCallback(
    (event: React.PointerEvent) => {
      event.preventDefault()
      dragging.current = true
      const move = (moved: PointerEvent) => {
        if (!dragging.current) return
        const raw =
          dock.side === 'right' ? window.innerWidth - moved.clientX : moved.clientX
        setDock((current) => ({ ...current, width: clampWidth(raw, window.innerWidth) }))
      }
      const done = () => {
        dragging.current = false
        window.removeEventListener('pointermove', move)
        window.removeEventListener('pointerup', done)
        // Written once, on release, rather than on every pointer event.
        setDock((current) => {
          try {
            window.localStorage.setItem(KEY, JSON.stringify(current))
          } catch {
            /* see above */
          }
          return current
        })
      }
      window.addEventListener('pointermove', move)
      window.addEventListener('pointerup', done)
    },
    [dock.side],
  )

  return { dock, setSide, toggle, startResize }
}
