/**
 * Saving where the user put things, without one tab losing another's work.
 *
 * The backend refuses a save against a stale revision and hands its current
 * layout back with the refusal (`backend/layout.py`). That refusal is the
 * whole design: it makes a two-tab conflict recoverable *here*, in code that
 * knows which nodes this tab actually moved.
 *
 * **Only this tab's own moves are re-asserted.** That is the subtle part. A
 * naive merge would write back every position the tab is holding, including
 * ones it merely read at load time — and if another tab has since moved one of
 * those, writing it back silently undoes them. So `mine` is the set of nodes
 * dragged *in this tab since it loaded*, and everything else comes from the
 * copy the server just returned.
 *
 *     merged = { ...theirs, ...mine }
 *
 * Per node, the later write wins. The file as a whole is never clobbered.
 */

import type { GraphLayout, Positions, SaveResult } from '../api/client'

export interface LayoutApi {
  layout(): Promise<GraphLayout>
  saveLayout(revision: number, positions: Positions): Promise<SaveResult>
}

/** Their layout, with this tab's own moves laid over it. */
export function mergePositions(theirs: Positions, mine: Positions): Positions {
  return { ...theirs, ...mine }
}

/**
 * Save `mine` on top of whatever the server currently holds.
 *
 * Retries bounded, and bounded low: each attempt is one more tab that got
 * there first. If several keep winning the race, the honest answer is to stop
 * and let the caller re-read rather than to spin.
 */
export async function saveMerging(
  api: LayoutApi,
  revision: number,
  mine: Positions,
  attempts = 3,
): Promise<GraphLayout> {
  let target = revision
  let positions = mine
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const answer = await api.saveLayout(target, positions)
    if (answer.ok) return answer.layout
    target = answer.current.revision
    positions = mergePositions(answer.current.positions, mine)
  }
  throw new Error(
    `the layout was saved by another tab ${attempts} times running; reload to pick up its copy`,
  )
}
