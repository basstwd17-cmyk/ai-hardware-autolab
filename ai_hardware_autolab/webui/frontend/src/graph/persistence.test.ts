/**
 * The two-tab case, which is the only reason this code is more than a PUT.
 *
 * The failure being tested for is silent: tab A moves a node, tab B saves, and
 * A's arrangement disappears without either of them being told. Every test
 * here is a shape of that.
 */

import { describe, expect, it, vi } from 'vitest'
import type { GraphLayout, Positions, SaveResult } from '../api/client'
import { mergePositions, saveMerging, type LayoutApi } from './persistence'

function layout(revision: number, positions: Positions): GraphLayout {
  return { version: 1, revision, positions, unreadable: null }
}

/** A backend that refuses a stale revision, the way the real one does. */
function server(initial: GraphLayout = layout(0, {})): LayoutApi & { held: GraphLayout } {
  const held = { current: initial }
  return {
    get held() {
      return held.current
    },
    layout: async () => held.current,
    saveLayout: async (revision, positions): Promise<SaveResult> => {
      if (revision !== held.current.revision) return { ok: false, current: held.current }
      held.current = layout(held.current.revision + 1, positions)
      return { ok: true, layout: held.current }
    },
  }
}

describe('merging', () => {
  it('lays this tab’s moves over theirs, per node', () => {
    const theirs = { a: { x: 1, y: 1 }, b: { x: 2, y: 2 } }
    const mine = { b: { x: 9, y: 9 }, c: { x: 3, y: 3 } }
    expect(mergePositions(theirs, mine)).toEqual({
      a: { x: 1, y: 1 },
      b: { x: 9, y: 9 },
      c: { x: 3, y: 3 },
    })
  })

  it('leaves the inputs alone', () => {
    const theirs = { a: { x: 1, y: 1 } }
    const mine = { b: { x: 2, y: 2 } }
    mergePositions(theirs, mine)
    expect(theirs).toEqual({ a: { x: 1, y: 1 } })
    expect(mine).toEqual({ b: { x: 2, y: 2 } })
  })
})

describe('saving against another tab', () => {
  it('saves straight through when nobody got there first', async () => {
    const api = server()
    const saved = await saveMerging(api, 0, { a: { x: 1, y: 1 } })
    expect(saved.revision).toBe(1)
    expect(saved.positions).toEqual({ a: { x: 1, y: 1 } })
  })

  it('recovers from a conflict without either tab losing a node', async () => {
    const api = server()
    // The other tab arranged two nodes while this one held revision 0.
    await api.saveLayout(0, { a: { x: 1, y: 1 }, b: { x: 2, y: 2 } })

    const saved = await saveMerging(api, 0, { c: { x: 3, y: 3 } })

    expect(saved.revision).toBe(2)
    expect(Object.keys(saved.positions).sort()).toEqual(['a', 'b', 'c'])
  })

  it('re-asserts only what this tab moved, never what it merely read', async () => {
    // The case a naive merge gets wrong: this tab loaded `a` at (1,1), never
    // touched it, and the other tab has since moved it. Writing back the copy
    // this tab is holding would silently undo them.
    const api = server()
    await api.saveLayout(0, { a: { x: 1, y: 1 } })
    await api.saveLayout(1, { a: { x: 50, y: 50 } }) // the other tab moved it

    const saved = await saveMerging(api, 1, { b: { x: 2, y: 2 } })

    expect(saved.positions.a).toEqual({ x: 50, y: 50 })
    expect(saved.positions.b).toEqual({ x: 2, y: 2 })
  })

  it('gives up rather than spinning when it keeps losing the race', async () => {
    const always: LayoutApi = {
      layout: async () => layout(7, {}),
      saveLayout: vi.fn(async () => ({ ok: false, current: layout(7, {}) })) as LayoutApi['saveLayout'],
    }
    await expect(saveMerging(always, 0, { a: { x: 1, y: 1 } }, 3)).rejects.toThrow(
      /another tab 3 times/,
    )
    expect(always.saveLayout).toHaveBeenCalledTimes(3)
  })
})
